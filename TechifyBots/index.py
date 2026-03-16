from pyrogram import Client, filters
from vars import DATABASE_CHANNEL_ID, ADMIN_IDS, ADMIN_ID
from Database.maindb import mdb
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
from pyrogram.errors import FloodWait, ChannelPrivate, ChatAdminRequired, UsernameNotOccupied
from datetime import datetime
from pyrogram.filters import create

INDEX_TASKS = {}

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


async def save_media(msg: Message, source_channel_id: int = None) -> bool:
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
        return result.upserted_id is not None  # True = newly inserted
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
        await save_media(message, source_channel_id=message.chat.id)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await save_media(message, source_channel_id=message.chat.id)
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
            result = await save_media(msg, source_channel_id=channel_id)
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
