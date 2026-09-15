from pyrogram import Client, filters
from vars import DATABASE_CHANNEL_ID, ADMIN_IDS, ADMIN_ID, R2_ENABLED, REELS_MAX_DURATION
from Database.maindb import mdb
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio, os, tempfile, re
from pyrogram.errors import FloodWait, ChannelPrivate, ChatAdminRequired, UsernameNotOccupied
from datetime import datetime
from pyrogram.filters import create
from . import r2_uploader

INDEX_TASKS = {}

# ==================== REELS WEBAPP MIRRORING ====================
# Runs as a background task per eligible video so it never slows down or
# blocks the indexing loop (real-time or bulk backfill). Bounded by a
# semaphore so we don't fire dozens of concurrent downloads/uploads during a
# big backfill and get flood-waited by Telegram or rate-limited by R2.
_MIRROR_SEMAPHORE = asyncio.Semaphore(3)


async def mirror_to_r2_if_eligible(client: Client, msg: Message, media_type: str, duration: int):
    """If R2 is configured and this video is short enough for reels, download
    it once via the bot and upload it to R2, then record it as reels-eligible.
    Any failure here is swallowed — it must never break indexing, and a video
    that fails to mirror simply stays DM-only (and un-flagged, so it can be
    retried later via /mirrorexisting)."""
    if not R2_ENABLED or media_type != "video":
        return
    if duration <= 0 or duration > REELS_MAX_DURATION:
        return

    async with _MIRROR_SEMAPHORE:
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            downloaded = await client.download_media(msg, file_name=tmp_path)
            if not downloaded:
                return
            key = f"reels/{msg.chat.id}_{msg.id}.mp4"
            size = await r2_uploader.upload_file(tmp_path, key)
            if size < 0:
                return
            await mdb.mark_reels_eligible(
                video_id=msg.id, source_channel_id=msg.chat.id,
                r2_key=key, r2_size=size,
            )
        except Exception as e:
            print(f"[mirror_to_r2_if_eligible] error for msg {getattr(msg, 'id', '?')}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

# ── helper: always returns ADMIN_IDS list ────────────────────────────────────
def _admin_list():
    return list(ADMIN_IDS)

# Convert single channel to list for filter
CHANNEL_LIST = DATABASE_CHANNEL_ID if isinstance(DATABASE_CHANNEL_ID, list) else [DATABASE_CHANNEL_ID]


# ==================== DB MIGRATION: fix old null channel_id docs ====================

@Client.on_message(filters.command("fix_index") & filters.private)
async def fix_index_nulls(client: Client, message: Message):
    """One-time migration: assign source_channel_id to old docs that have channel_id=null."""
    if message.from_user.id not in _admin_list():
        await message.reply_text("**🚫 Not authorized.**")
        return

    msg = await message.reply_text("🔧 Scanning for old docs with `channel_id: null`...")
    count = await mdb.async_video_collection.count_documents({"channel_id": None})
    if count == 0:
        await msg.edit_text("✅ No old null-channel docs found. DB is clean!")
        return

    await msg.edit_text(
        f"⚠️ Found **{count}** old docs with `channel_id: null`.\n\n"
        f"These were indexed before multi-channel support was added.\n"
        f"Use `/reindex` to re-index your channels from scratch, or "
        f"send the channel ID to assign them to:\n\n"
        f"Reply with a channel ID (e.g. `-1001234567890`) to bulk-assign, "
        f"or `/skip_fix` to leave them as-is (they won't appear in category filters)."
    )


# ==================== SAVE MEDIA ====================

def _extract_media(msg: Message):
    """
    Returns (media_object, media_type, duration) for any supported media.
    For photos (pyrofork), msg.photo is a Photo object (not a list).
    """
    if msg.video:
        return msg.video, "video", msg.video.duration or 0
    if msg.photo:
        # pyrofork: msg.photo is a Photo object with .file_id
        return msg.photo, "photo", 0
    if msg.document:
        return msg.document, "document", 0
    if msg.audio:
        return msg.audio, "audio", msg.audio.duration or 0
    if msg.voice:
        return msg.voice, "voice", msg.voice.duration or 0
    if msg.animation:
        return msg.animation, "animation", msg.animation.duration or 0
    if msg.sticker:
        return msg.sticker, "sticker", 0
    return None, None, 0


async def save_media(msg: Message, source_channel_id: int = None, client: Client = None) -> bool:
    """
    Save any media type to the DB. Returns True if newly saved, False if duplicate/no-media.
    Always stores source_channel_id so category filtering works.

    FIX: Old DB had a unique index on (video_id, channel_id) with channel_id=null for legacy docs.
    We now use update_one with upsert keyed on (video_id, source_channel_id) to avoid E11000 errors,
    and also store 'channel_id' field = source_channel_id so the old index is satisfied.
    """
    media, media_type, duration = _extract_media(msg)
    if not media:
        return False

    channel_id = source_channel_id or (msg.chat.id if msg.chat else None)

    doc = {
        "video_id": msg.id,
        "file_id": media.file_id,
        "media_type": media_type,
        "duration": duration,
        "source_channel_id": channel_id,
        "channel_id": channel_id,   # kept for old index compatibility
        "added_at": datetime.now(),
    }

    try:
        result = await mdb.async_video_collection.update_one(
            {"video_id": msg.id, "source_channel_id": channel_id},
            {"$setOnInsert": doc},
            upsert=True,
        )
        is_new = result.upserted_id is not None
        if is_new and media_type == "video" and client is not None:
            asyncio.create_task(mirror_to_r2_if_eligible(client, msg, media_type, duration))
        return is_new  # True = newly inserted
    except Exception as e:
        err_str = str(e)
        # If it's a dup key on the OLD (video_id, channel_id) index,
        # update that old doc to add source_channel_id and correct fields
        if "11000" in err_str:
            try:
                await mdb.async_video_collection.update_one(
                    {"video_id": msg.id, "channel_id": None},
                    {"$set": {
                        "source_channel_id": channel_id,
                        "channel_id": channel_id,
                        "file_id": media.file_id,
                        "media_type": media_type,
                    }}
                )
                return False  # already existed (migrated)
            except Exception:
                pass
        print(f"[save_media] error: {e}")
        return False


# ==================== AUTO INDEX (MULTI-CHANNEL) ====================

@Client.on_message(
    filters.chat(CHANNEL_LIST) &
    (filters.video | filters.photo | filters.document |
     filters.audio | filters.voice | filters.animation)
)
async def auto_index(client: Client, message: Message):
    """Auto-index from all database channels"""
    try:
        await save_media(message, source_channel_id=message.chat.id, client=client)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await save_media(message, source_channel_id=message.chat.id, client=client)
        except Exception as ex:
            print(f"[auto_index] retry error: {ex}")
    except Exception as e:
        print(f"[auto_index] error: {e}")


# ==================== MANUAL INDEX ====================

@Client.on_message(filters.command("index") & filters.private)
async def manual_index(client: Client, message: Message):
    """Select channel to index — admin only"""
    if message.from_user.id not in _admin_list():
        await message.reply_text("**🚫 You're not authorized to use this command.**")
        return

    channels = CHANNEL_LIST
    if not channels:
        await message.reply_text("**❌ No DATABASE_CHANNEL_ID configured.**")
        return

    status_msg = await message.reply_text("⏳ Fetching channel list...")

    buttons = []
    failed_channels = []
    for ch in channels:
        try:
            chat = await client.get_chat(ch)
            title = chat.title or str(ch)
            count_in_db = await mdb.async_video_collection.count_documents(
                {"source_channel_id": ch}
            )
            buttons.append([
                InlineKeyboardButton(
                    f"{title} ({count_in_db} indexed)",
                    callback_data=f"index_select_{ch}"
                )
            ])
        except (ChannelPrivate, ChatAdminRequired) as e:
            failed_channels.append(f"`{ch}` — bot not admin/member")
        except Exception as e:
            failed_channels.append(f"`{ch}` — {type(e).__name__}: {e}")

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")])

    text_out = "📂 **Select a channel to index:**\n"
    if failed_channels:
        text_out += "\n⚠️ **Could not fetch these channels (bot must be admin):**\n"
        text_out += "\n".join(failed_channels) + "\n"

    if len(buttons) == 1:  # only Cancel button
        await status_msg.edit_text(
            "❌ **No accessible channels found.**\n\n"
            "Make sure the bot is an **admin** in all DATABASE_CHANNEL_ID channels.\n\n" +
            ("\n".join(failed_channels) if failed_channels else "")
        )
        return

    await status_msg.edit_text(
        text_out,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ==================== SKIP NUMBER ====================

def is_waiting_skip(_, __, message):
    if not message.from_user:
        return False
    data = INDEX_TASKS.get(message.from_user.id)
    return bool(data and data.get("state") == "await_skip")

skip_filter = create(is_waiting_skip)


@Client.on_message(filters.private & filters.text & skip_filter)
async def skip_number(client: Client, message: Message):
    """Receive skip message ID"""
    if not message.from_user or message.from_user.id not in _admin_list():
        return
    if message.text.startswith("/"):
        return

    data = INDEX_TASKS.get(message.from_user.id)
    if not data or data.get("state") != "await_skip":
        return

    channel_id = data["channel_id"]
    raw = message.text.strip()

    # Accept "0" to start from beginning, link, or plain number
    if raw == "0":
        skip_id = 0
    elif "t.me" in raw:
        try:
            skip_id = int(raw.rstrip("/").split("/")[-1])
        except Exception:
            await message.reply_text("❌ Invalid link — could not parse message ID.")
            return
    elif raw.lstrip("-").isdigit():
        skip_id = int(raw)
    else:
        await message.reply_text("❌ Invalid input. Send a message ID number, 0, or a t.me link.")
        return

    await message.delete()

    try:
        progress = await client.get_messages(message.chat.id, data["msg_id"])
    except Exception:
        progress = await message.reply_text("⏳ Starting...")

    await progress.edit_text(
        f"⏳ **Starting index...**\n\nChannel: `{channel_id}`\nStarting from ID: `{skip_id}`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]])
    )

    INDEX_TASKS[message.from_user.id] = {
        "channel_id": channel_id,
        "skip_id": skip_id,
        "state": "indexing",
        "cancel": False,
        "progress_msg": progress
    }

    asyncio.create_task(start_indexing(client, message.from_user.id))


# ==================== INDEXING WORKER ====================

async def start_indexing(client: Client, user_id: int):
    """Index channel messages from skip_id upward."""
    data = INDEX_TASKS.get(user_id)
    if not data:
        return

    channel_id = data["channel_id"]
    skip_id = data["skip_id"]
    progress = data["progress_msg"]

    saved = duplicate = skipped = error = count = 0
    # Start from message 1 if skip_id==0, otherwise skip_id+1
    current_id = 1 if skip_id == 0 else skip_id + 1
    consecutive_missing = 0
    max_missing = 150  # stop after 150 consecutive empty/deleted messages

    while True:
        if INDEX_TASKS.get(user_id, {}).get("cancel"):
            try:
                await progress.edit_text(
                    f"❌ **Indexing Cancelled**\n\n"
                    f"Processed: {count} | Saved: {saved} | Duplicate: {duplicate} | Errors: {error}"
                )
            except Exception:
                pass
            INDEX_TASKS.pop(user_id, None)
            return

        try:
            msg = await client.get_messages(channel_id, current_id)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception as e:
            print(f"[indexing] get_messages error at {current_id}: {e}")
            consecutive_missing += 1
            skipped += 1
            current_id += 1
            if consecutive_missing >= max_missing:
                break
            continue

        if not msg or msg.empty:
            consecutive_missing += 1
            skipped += 1
            current_id += 1
            if consecutive_missing >= max_missing:
                break
            continue

        consecutive_missing = 0

        try:
            result = await save_media(msg, source_channel_id=channel_id, client=client)
            if result:
                saved += 1
            else:
                duplicate += 1
        except Exception as e:
            print(f"[indexing] save_media error at {current_id}: {e}")
            error += 1

        count += 1
        current_id += 1

        # Yield to event loop every 50 messages to avoid blocking
        if count % 50 == 0:
            await asyncio.sleep(0.05)

        # Update progress every 25 messages
        if count % 25 == 0:
            try:
                await progress.edit_text(
                    f"📂 **Indexing in progress...**\n\n"
                    f"Channel: `{channel_id}`\n"
                    f"Current ID: `{current_id - 1}`\n\n"
                    f"✅ Saved: **{saved}**\n"
                    f"♻️ Duplicate: **{duplicate}**\n"
                    f"⏭️ Skipped/Empty: **{skipped}**\n"
                    f"⚠️ Errors: **{error}**\n"
                    f"📊 Total Processed: **{count}**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]])
                )
            except Exception:
                pass

    # Final report
    try:
        await progress.edit_text(
            f"✅ **Indexing Complete!**\n\n"
            f"Channel: `{channel_id}`\n\n"
            f"✅ Saved: **{saved}**\n"
            f"♻️ Duplicate: **{duplicate}**\n"
            f"⏭️ Skipped/Empty: **{skipped}**\n"
            f"⚠️ Errors: **{error}**\n"
            f"📊 Total Processed: **{count}**",
            reply_markup=None
        )
    except Exception:
        pass

    INDEX_TASKS.pop(user_id, None)


# ==================== REELS BACKFILL (channel + range picker) ====================
# /mirrorexisting: admin picks a channel, then gives an end point (forward the
# newest message to mirror up to, or send its link) and a start point (a bare
# message-ID number, or a link) — mirrors just that range to R2, instead of
# scanning every channel. Follows the same state-dict + callback pattern as
# /index above (MIRROR_TASKS mirrors INDEX_TASKS's shape on purpose).

MIRROR_TASKS: dict[int, dict] = {}

_MIRROR_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(c/)?([A-Za-z0-9_]+)/(\d+)(?:[/?].*)?$", re.IGNORECASE
)


def _parse_mirror_link(target: str):
    """Parse a t.me message link into (channel_ref, msg_id). channel_ref is
    an int chat_id for '/c/' links (with -100 prefix already applied), or a
    str username for public links. None if not a recognizable link."""
    m = _MIRROR_LINK_RE.search(target.strip())
    if not m:
        return None
    is_private, ident, mid = m.group(1), m.group(2), m.group(3)
    if is_private:
        if not ident.isdigit():
            return None
        return int(f"-100{ident}"), int(mid)
    return ident, int(mid)


async def _extract_ref(client: Client, message: Message):
    """From an incoming message, get (channel_id, msg_id) — either from a
    forwarded-with-origin-intact message, or a t.me link in the text.
    Returns None if neither is present/resolvable."""
    if message.forward_from_chat and message.forward_from_message_id:
        return message.forward_from_chat.id, message.forward_from_message_id
    if message.text:
        parsed = _parse_mirror_link(message.text)
        if parsed:
            channel_ref, mid = parsed
            if isinstance(channel_ref, int):
                return channel_ref, mid
            try:
                chat = await client.get_chat(channel_ref)
                return chat.id, mid
            except Exception:
                return None
    return None


@Client.on_message(filters.command("mirrorexisting") & filters.private)
async def mirror_existing_command(client: Client, message: Message):
    """Admin-only: pick a channel + message-ID range to mirror to R2."""
    if message.from_user.id not in _admin_list():
        await message.reply_text("**🚫 You're not authorized to use this command.**")
        return
    if not R2_ENABLED:
        await message.reply_text("❌ R2 isn't configured (missing R2_* env vars) — nothing to backfill.")
        return

    status_msg = await message.reply_text("⏳ Fetching channel list...")
    buttons = []
    failed_channels = []
    for ch in CHANNEL_LIST:
        try:
            chat = await client.get_chat(ch)
            title = chat.title or str(ch)
            pending = await mdb.async_video_collection.count_documents({
                "source_channel_id": ch,
                "media_type": "video",
                "duration": {"$gt": 0, "$lte": REELS_MAX_DURATION},
                "reels_eligible": {"$ne": True},
            })
            buttons.append([InlineKeyboardButton(
                f"{title} ({pending} pending)", callback_data=f"mirrorsel_{ch}"
            )])
        except (ChannelPrivate, ChatAdminRequired):
            failed_channels.append(f"`{ch}` — bot not admin/member")
        except Exception as e:
            failed_channels.append(f"`{ch}` — {type(e).__name__}: {e}")

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="mirrorcancel")])

    if len(buttons) == 1:
        await status_msg.edit_text(
            "❌ **No accessible channels found.**\n\n" + "\n".join(failed_channels)
        )
        return

    text_out = "📂 **Select a channel to mirror reels from:**\n"
    if failed_channels:
        text_out += "\n⚠️ **Couldn't fetch these:**\n" + "\n".join(failed_channels) + "\n"
    await status_msg.edit_text(text_out, reply_markup=InlineKeyboardMarkup(buttons))


def _is_awaiting_mirror(state: str):
    def _check(_, __, message):
        if not message.from_user:
            return False
        task = MIRROR_TASKS.get(message.from_user.id)
        return bool(task and task.get("state") == state and not (message.text and message.text.startswith("/")))
    return create(_check)


@Client.on_message(filters.private & _is_awaiting_mirror("await_end"))
async def mirror_receive_end(client: Client, message: Message):
    task = MIRROR_TASKS.get(message.from_user.id)
    if not task:
        return
    ref = await _extract_ref(client, message)
    if not ref:
        await message.reply_text(
            "❌ I need either a **forwarded message** (with the channel tag visible, not "
            "forwarded anonymously) or a **t.me link** to that message. Try again."
        )
        return
    channel_id, end_id = ref
    if channel_id != task["channel_id"]:
        await message.reply_text(
            f"⚠️ That message is from a different channel (`{channel_id}`) than the one you "
            f"selected (`{task['channel_id']}`). Forward a message from the selected channel."
        )
        return

    task["end_id"] = end_id
    task["state"] = "await_start"
    await message.reply_text(
        f"✅ End point set: message `{end_id}`.\n\n"
        f"Now send the **starting point** — a plain number (e.g. `250`) to start from that "
        f"message ID, or a t.me link/forwarded message for the start."
    )


@Client.on_message(filters.private & _is_awaiting_mirror("await_start"))
async def mirror_receive_start(client: Client, message: Message):
    task = MIRROR_TASKS.get(message.from_user.id)
    if not task:
        return

    raw = (message.text or "").strip()
    start_id = None
    if raw.isdigit():
        start_id = int(raw)
    else:
        ref = await _extract_ref(client, message)
        if ref and ref[0] == task["channel_id"]:
            start_id = ref[1]

    if start_id is None:
        await message.reply_text(
            "❌ Send a plain message-ID number (e.g. `250`), or a t.me link/forwarded "
            "message from the selected channel."
        )
        return

    end_id = task["end_id"]
    if start_id > end_id:
        start_id, end_id = end_id, start_id  # forgiving swap if given backwards

    channel_id = task["channel_id"]
    progress = await message.reply_text(
        f"⏳ **Mirroring started.**\nChannel: `{channel_id}`\nRange: `{start_id}` → `{end_id}`"
    )
    MIRROR_TASKS[message.from_user.id] = {
        "state": "running", "channel_id": channel_id,
        "start_id": start_id, "end_id": end_id, "progress_msg": progress,
    }
    asyncio.create_task(run_ranged_mirror(client, message.from_user.id))


async def run_ranged_mirror(client: Client, user_id: int):
    task = MIRROR_TASKS.get(user_id)
    if not task:
        return
    channel_id, start_id, end_id = task["channel_id"], task["start_id"], task["end_id"]
    progress = task["progress_msg"]

    total_mirrored = 0
    stuck_batches = 0
    while True:
        batch = await mdb.get_reels_eligible_range(channel_id, start_id, end_id, REELS_MAX_DURATION, batch_size=25)
        if not batch:
            break
        mirrored_before = total_mirrored
        for doc in batch:
            vid_id = doc.get("video_id")
            if vid_id is None:
                continue
            try:
                msg = await client.get_messages(channel_id, vid_id)
                if msg and not msg.empty and msg.video:
                    await mirror_to_r2_if_eligible(client, msg, "video", doc.get("duration", 0))
                    total_mirrored += 1
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                print(f"[mirrorexisting] error on video_id={vid_id}: {e}")
        try:
            await progress.edit_text(f"⏳ Mirrored **{total_mirrored}** so far (range `{start_id}`–`{end_id}`)...")
        except Exception:
            pass
        if total_mirrored == mirrored_before:
            stuck_batches += 1
            if stuck_batches >= 3:
                await progress.edit_text(
                    f"⚠️ **Stopped.** Mirrored **{total_mirrored}**, but the remaining videos in "
                    f"this range keep failing (check logs / R2 credentials)."
                )
                MIRROR_TASKS.pop(user_id, None)
                return
        else:
            stuck_batches = 0
        await asyncio.sleep(0.5)

    await progress.edit_text(
        f"✅ **Done.** Mirrored **{total_mirrored}** video(s) from channel `{channel_id}`, "
        f"range `{start_id}`–`{end_id}`."
    )
    MIRROR_TASKS.pop(user_id, None)
