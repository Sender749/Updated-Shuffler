from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from vars import ADMIN_ID, DELETE_TIMER, PROTECT_CONTENT, POST_CHANNEL
from Database.maindb import mdb
import string
import random
import os
import asyncio
import tempfile, shutil
from datetime import datetime

# Store temporary link generation sessions
LINK_SESSIONS = {}

# Store screenshot navigation sessions for admin
SCREENSHOT_SESSIONS = {}

# Lock per user to prevent race conditions in collect_files
_COLLECT_LOCKS = {}


def generate_link_id():
    """Generate a unique 8-character link ID"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))


def _get_collect_lock(user_id: int) -> asyncio.Lock:
    """Return (creating if needed) a per-user asyncio lock."""
    if user_id not in _COLLECT_LOCKS:
        _COLLECT_LOCKS[user_id] = asyncio.Lock()
    return _COLLECT_LOCKS[user_id]


# ==================== START LINK GENERATION ====================

@Client.on_message(filters.command("l") & filters.private & filters.user(ADMIN_ID))
async def start_link_generation(client: Client, message: Message):
    """Start the link generation process."""
    user_id = message.from_user.id

    # Reset session
    LINK_SESSIONS[user_id] = {
        "files": [],
        "state": "collecting",
        "count_msg_id": None,
        "count_chat_id": message.chat.id,
    }

    sent = await message.reply_text(
        "**📁 Send files to generate link**\n\n"
        "Files collected: **0**\n\n"
        "Use /m_link to generate screenshots & link"
    )
    LINK_SESSIONS[user_id]["count_msg_id"] = sent.id


# ==================== COLLECT FILES ====================

@Client.on_message(
    filters.private &
    filters.user(ADMIN_ID) &
    (filters.video | filters.photo | filters.document | filters.animation)
)
async def collect_files(client: Client, message: Message):
    """Collect files from admin — one count msg updated per batch, no duplicates."""
    user_id = message.from_user.id

    # --- Handle custom screenshot upload (awaiting photo from admin) ---
    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if ss_session and ss_session.get("state") == "awaiting_custom_photo":
        if message.photo:
            await _handle_custom_photo(client, message, user_id, ss_session)
        else:
            await message.reply_text("❌ Please send a **photo** for custom screenshot.")
        return

    # --- Normal file collection: serialize with a per-user lock to avoid race ---
    if user_id not in LINK_SESSIONS or LINK_SESSIONS[user_id]["state"] != "collecting":
        return

    lock = _get_collect_lock(user_id)
    async with lock:
        # Re-check session inside lock (could have been cleared)
        if user_id not in LINK_SESSIONS or LINK_SESSIONS[user_id]["state"] != "collecting":
            return

        session = LINK_SESSIONS[user_id]

        # Extract file info
        file_info = None
        if message.animation:
            # GIF/animation — treat as video
            file_info = {
                "type": "video",
                "file_id": message.animation.file_id,
                "duration": message.animation.duration or 0,
                "caption": message.caption or "",
            }
        elif message.video:
            file_info = {
                "type": "video",
                "file_id": message.video.file_id,
                "duration": message.video.duration or 0,
                "caption": message.caption or "",
            }
        elif message.photo:
            file_info = {
                "type": "photo",
                "file_id": message.photo.file_id,
                "duration": 0,
                "caption": message.caption or "",
            }
        elif message.document:
            mime = getattr(message.document, "mime_type", "") or ""
            file_info = {
                "type": "document",
                "file_id": message.document.file_id,
                "file_name": getattr(message.document, "file_name", "file") or "file",
                "mime_type": mime,
                # Try to get duration from video attribute if document is a video
                "duration": 0,
                "caption": message.caption or "",
            }

        if file_info is None:
            return

        session["files"].append(file_info)
        count = len(session["files"])
        chat_id = session["count_chat_id"]
        old_msg_id = session.get("count_msg_id")

        new_text = (
            "**📁 Send files to generate link**\n\n"
            f"Files collected: **{count}**\n\n"
            "Use /m_link to generate screenshots & link"
        )

        # Try to edit the existing count message in-place
        edited = False
        if old_msg_id:
            try:
                await client.edit_message_text(
                    chat_id=chat_id,
                    message_id=old_msg_id,
                    text=new_text,
                )
                edited = True
            except Exception:
                # Message may have been deleted or is too old — fall through to send new
                pass

        if not edited:
            # Delete old if it exists (best-effort)
            if old_msg_id:
                try:
                    await client.delete_messages(chat_id, old_msg_id)
                except Exception:
                    pass
            # Send fresh count message
            new_msg = await message.reply_text(new_text)
            session["count_msg_id"] = new_msg.id


# ==================== GENERATE SCREENSHOTS & LINK ====================

@Client.on_message(filters.command("m_link") & filters.private & filters.user(ADMIN_ID))
async def generate_multi_link(client: Client, message: Message):
    """Generate screenshots from collected files, show navigation to admin."""
    user_id = message.from_user.id

    if user_id not in LINK_SESSIONS:
        await message.reply_text("❌ No active session. Use /l to start.")
        return

    session = LINK_SESSIONS[user_id]
    if not session["files"]:
        await message.reply_text("❌ No files collected. Send files first.")
        return

    # Delete the count message and use /m_link message as status carrier
    count_msg_id = session.get("count_msg_id")
    if count_msg_id:
        try:
            await client.delete_messages(session["count_chat_id"], count_msg_id)
        except Exception:
            pass

    # Edit the /m_link command message as status (delete it first since it's a command)
    status_msg = await message.reply_text(
        "⏳ **Generating screenshots from your files…**\n\nThis may take a moment."
    )

    used_timestamps = []
    try:
        screenshots = await generate_screenshots(
            client, session["files"], used_timestamps, status_msg=status_msg
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Screenshot generation failed:\n`{e}`")
        return

    if not screenshots:
        await status_msg.edit_text(
            "❌ Could not generate screenshots.\n\n"
            "Make sure the files you sent are **video files** (not photos or unsupported formats)."
        )
        return

    # Generate and store link in DB
    link_id = generate_link_id()
    await mdb.async_db["file_links"].insert_one({
        "link_id": link_id,
        "files": session["files"],
        "created_by": user_id,
        "created_at": datetime.now(),
        "access_count": 0,
    })
    bot_info = await client.get_me()
    link = f"https://t.me/{bot_info.username}?start=link_{link_id}"

    # Init screenshot browsing session
    SCREENSHOT_SESSIONS[user_id] = {
        "screenshots": screenshots,
        "used_timestamps": used_timestamps,
        "current_index": 0,
        "link": link,
        "link_id": link_id,
        "source_files": session["files"],
        "state": "browsing",
        "nav_msg_id": None,
        "nav_chat_id": message.chat.id,
    }

    del LINK_SESSIONS[user_id]

    # Delete status msg and send screenshot navigator
    await status_msg.delete()
    await show_screenshot(client, message.chat.id, user_id, send_new=True)


# ==================== SHOW SCREENSHOT WITH NAVIGATION ====================

async def show_screenshot(client: Client, chat_id: int, user_id: int, send_new: bool = False):
    """Send or edit the screenshot navigation message."""
    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if not ss_session:
        return

    idx = ss_session["current_index"]
    screenshots = ss_session["screenshots"]
    total = len(screenshots)
    link = ss_session["link"]
    photo_path = screenshots[idx]

    caption = (
        f"🖼 **Screenshot {idx + 1} of {total}**\n\n"
        f"🔗 Link: `{link}`\n\n"
        "Browse, customise, or post to channel using the buttons below."
    )

    buttons = [
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"ss_back_{user_id}"),
            InlineKeyboardButton(f"📸 {idx + 1}/{total}", callback_data="ss_noop"),
            InlineKeyboardButton("➡️ Next", callback_data=f"ss_next_{user_id}"),
        ],
        [
            InlineKeyboardButton("🎨 Custom", callback_data=f"ss_custom_{user_id}"),
            InlineKeyboardButton("🔄 Generate More", callback_data=f"ss_gen_{user_id}"),
        ],
        [
            InlineKeyboardButton("📤 Send to Channel", callback_data=f"ss_send_{user_id}"),
        ],
    ]
    markup = InlineKeyboardMarkup(buttons)
    nav_msg_id = ss_session.get("nav_msg_id")

    if not send_new and nav_msg_id:
        # Try editing existing message in-place
        try:
            await client.edit_message_media(
                chat_id=chat_id,
                message_id=nav_msg_id,
                media=InputMediaPhoto(media=photo_path, caption=caption),
                reply_markup=markup,
            )
            return
        except Exception:
            # Fall through to delete + send new
            try:
                await client.delete_messages(chat_id, nav_msg_id)
            except Exception:
                pass
            ss_session["nav_msg_id"] = None

    # Send a fresh photo message
    try:
        sent = await client.send_photo(
            chat_id, photo=photo_path, caption=caption, reply_markup=markup
        )
        ss_session["nav_msg_id"] = sent.id
    except Exception as e:
        print(f"[show_screenshot] send error: {e}")


# ==================== GENERATE SCREENSHOTS HELPER ====================

async def _run_async(cmd: list, timeout: int = 120) -> tuple:
    """Run a subprocess asynchronously, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"


async def generate_screenshots(
    client: Client,
    files: list,
    used_timestamps: list = None,
    max_shots: int = 20,
    status_msg=None,
) -> list:
    """
    Download video files and extract screenshots via ffmpeg (async subprocesses).
    used_timestamps: list of (file_index, float_ts) tuples already used — avoids repeats.
    Returns list of local file paths to the JPEG screenshots.
    """
    if used_timestamps is None:
        used_timestamps = []

    # Only process video-capable files; photos can't yield screenshots
    video_files = [
        (i, f) for i, f in enumerate(files)
        if f["type"] in ("video", "document")
    ]

    if not video_files:
        return []

    # --- Distribute screenshot quota proportionally by metadata duration ---
    total_meta_dur = sum(max(f.get("duration", 0), 0) for _, f in video_files)

    screenshots_per_file: dict[int, int] = {}
    if total_meta_dur > 0:
        for orig_idx, f in video_files:
            dur = max(f.get("duration", 0), 1)
            share = dur / total_meta_dur
            screenshots_per_file[orig_idx] = max(1, round(share * max_shots))
    else:
        per = max(1, max_shots // len(video_files))
        for orig_idx, _ in video_files:
            screenshots_per_file[orig_idx] = per

    # Cap total to max_shots
    total_assigned = sum(screenshots_per_file.values())
    if total_assigned > max_shots:
        scale = max_shots / total_assigned
        for k in screenshots_per_file:
            screenshots_per_file[k] = max(1, round(screenshots_per_file[k] * scale))

    all_screenshots: list[str] = []
    tmpdir = tempfile.mkdtemp(prefix="bot_ss_")

    for file_number, (orig_idx, f) in enumerate(video_files, start=1):
        file_id = f["file_id"]
        want = screenshots_per_file.get(orig_idx, 1)

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ **Generating screenshots…**\n\n"
                    f"Downloading file {file_number}/{len(video_files)}…"
                )
            except Exception:
                pass

        # ---- Download file ----
        dl_path = None
        try:
            # Give the file a proper name so ffmpeg/ffprobe can identify it
            fname = f.get("file_name") or f"video_{orig_idx}.mp4"
            if "." not in os.path.basename(fname):
                fname += ".mp4"
            dest = os.path.join(tmpdir, f"file_{orig_idx}_{fname}")
            dl_path = await client.download_media(file_id, file_name=dest)
        except Exception as e:
            print(f"[generate_screenshots] download error for file {orig_idx}: {e}")
            continue

        if not dl_path or not os.path.exists(dl_path):
            print(f"[generate_screenshots] dl_path invalid: {dl_path!r}")
            continue

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ **Generating screenshots…**\n\n"
                    f"Probing file {file_number}/{len(video_files)}…"
                )
            except Exception:
                pass

        # ---- Get actual duration via ffprobe ----
        actual_dur = 0.0
        rc, stdout, stderr = await _run_async(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                dl_path,
            ],
            timeout=60,
        )
        if rc == 0 and stdout.strip():
            try:
                actual_dur = float(stdout.strip())
            except ValueError:
                pass

        if actual_dur <= 0:
            # Fall back to metadata duration
            actual_dur = max(f.get("duration", 0) or 0, 0)

        if actual_dur <= 0:
            print(f"[generate_screenshots] could not determine duration for file {orig_idx}, skipping")
            try:
                os.remove(dl_path)
            except Exception:
                pass
            continue

        # ---- Determine how many screenshots to take ----
        # Cap by duration: at most 1 shot per second, but no more than `want`
        adj_want = min(want, max(1, int(actual_dur)))

        # Get timestamps to avoid (already extracted from this file)
        used_for_file = {ts for (fi, ts) in used_timestamps if fi == orig_idx}
        timestamps = _pick_timestamps(actual_dur, adj_want, used_for_file)

        if not timestamps:
            print(f"[generate_screenshots] no timestamps for file {orig_idx} (dur={actual_dur})")
            try:
                os.remove(dl_path)
            except Exception:
                pass
            continue

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ **Generating screenshots…**\n\n"
                    f"Extracting {len(timestamps)} frames from file {file_number}/{len(video_files)}…"
                )
            except Exception:
                pass

        # ---- Extract frames concurrently ----
        async def extract_frame(ts: float, out_path: str) -> bool:
            rc2, _, err2 = await _run_async(
                [
                    "ffmpeg",
                    "-ss", f"{ts:.3f}",
                    "-i", dl_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    "-f", "image2",
                    out_path,
                    "-y",
                ],
                timeout=60,
            )
            if rc2 == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return True
            print(f"[extract_frame] ffmpeg failed rc={rc2} ts={ts:.3f}: {err2[-200:]}")
            return False

        tasks = []
        frame_paths = []
        for ts in timestamps:
            out_path = os.path.join(tmpdir, f"ss_{orig_idx}_{ts:.3f}.jpg")
            frame_paths.append((ts, out_path))
            tasks.append(extract_frame(ts, out_path))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (ts, out_path), ok in zip(frame_paths, results):
            if ok is True:
                all_screenshots.append(out_path)
                used_timestamps.append((orig_idx, ts))

        try:
            os.remove(dl_path)
        except Exception:
            pass

    return all_screenshots


def _pick_timestamps(duration: float, count: int, exclude: set = None) -> list:
    """
    Pick `count` evenly-spaced timestamps between 5% and 95% of `duration`,
    avoiding timestamps already in `exclude` (within 1 second).
    """
    if duration <= 0 or count <= 0:
        return []

    exclude = exclude or set()
    start = duration * 0.05
    end = duration * 0.95
    if end <= start:
        start = 0.0
        end = duration

    # Build a dense candidate pool then sample evenly from it
    pool_size = max(count * 6, 40)
    step = (end - start) / pool_size
    candidates = []
    t = start
    while t <= end + step * 0.5:
        rounded = round(t, 3)
        if not any(abs(rounded - ex) < 1.0 for ex in exclude):
            candidates.append(rounded)
        t += step

    if not candidates:
        return []
    if len(candidates) <= count:
        return candidates

    # Evenly sample `count` items from the candidate list
    if count == 1:
        return [candidates[len(candidates) // 2]]
    indices = {round(i * (len(candidates) - 1) / (count - 1)) for i in range(count)}
    return [candidates[i] for i in sorted(indices)]


# ==================== CUSTOM PHOTO HANDLER ====================

async def _handle_custom_photo(client: Client, message: Message, user_id: int, ss_session: dict):
    """Admin sent a custom photo — download it and insert at current position."""
    photo = message.photo
    tmpdir = tempfile.mkdtemp(prefix="bot_custom_")
    photo_path = os.path.join(tmpdir, "custom_photo.jpg")

    try:
        await client.download_media(photo.file_id, file_name=photo_path)
    except Exception as e:
        await message.reply_text(f"❌ Failed to download photo: {e}")
        return

    if not os.path.exists(photo_path) or os.path.getsize(photo_path) == 0:
        await message.reply_text("❌ Downloaded photo is empty. Please try again.")
        return

    # Insert at current index so it becomes the visible screenshot
    idx = ss_session["current_index"]
    ss_session["screenshots"].insert(idx, photo_path)
    ss_session["state"] = "browsing"

    # Edit nav message to show new screenshot
    chat_id = ss_session.get("nav_chat_id", message.chat.id)
    await show_screenshot(client, chat_id, user_id)


# ==================== POST TO CHANNEL ====================

async def post_screenshot_to_channel(client: Client, chat_id: int, user_id: int, query=None):
    """Send the current screenshot to POST_CHANNEL with a Generate Link button."""
    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if not ss_session:
        if query:
            await query.answer("❌ No active session.", show_alert=True)
        return

    if not POST_CHANNEL:
        if query:
            await query.answer("❌ POST_CHANNEL not configured.", show_alert=True)
        return

    idx = ss_session["current_index"]
    photo_path = ss_session["screenshots"][idx]
    link = ss_session["link"]

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Get Files", url=link)
    ]])

    try:
        await client.send_photo(
            POST_CHANNEL,
            photo=photo_path,
            caption="📥 **Click the button below to get the files!**",
            reply_markup=markup,
        )
        screenshots = ss_session.get("screenshots", [])
        temp_dirs = set()
        for path in screenshots:
            try:
                if os.path.exists(path):
                    temp_dirs.add(os.path.dirname(path))
                except Exception:
                    pass
        for d in temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception as e:
                print(f"[cleanup] failed to remove dir {d}: {e}")
        SCREENSHOT_SESSIONS.pop(user_id, None)
        if query:
            await query.answer("✅ Posted to channel!", show_alert=True)
        else:
            await client.send_message(chat_id, "✅ Screenshot **posted to channel** with Get Files button!")
    except Exception as e:
        err = f"❌ Failed to post: `{e}`"
        if query:
            await query.answer(err[:200], show_alert=True)
        else:
            await client.send_message(chat_id, err)


# ==================== HANDLE LINK ACCESS ====================

async def handle_link_access(client: Client, message: Message, link_id: str):
    """Handle when a user accesses a generated link via /start link_<id>."""
    user_id = message.from_user.id

    link_data = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not link_data:
        await message.reply_text("❌ Invalid or expired link.")
        return

    files = link_data.get("files", [])
    if not files:
        await message.reply_text("❌ No files found in this link.")
        return

    from .cmds import (
        get_cached_user_data, get_cached_verification, show_verify,
        USER_ACTIVE_VIDEOS, auto_delete, USER_CURRENT_VIDEO,
    )
    from .fsub import get_fsub
    from vars import IS_FSUB, IS_VERIFY
    from Database.userdb import udb

    if await udb.is_user_banned(user_id):
        await message.reply_text("**🚫 You are banned**")
        return

    if IS_FSUB and not await get_fsub(client, message):
        return

    user = await get_cached_user_data(user_id)
    is_prime = user.get("plan") == "prime"

    if not is_prime:
        if IS_VERIFY:
            verified, is_second, is_third = await get_cached_verification(user_id)
            if not verified or is_second or is_third:
                usage = await mdb.check_and_increment_usage(user_id)
                if not usage["allowed"]:
                    await show_verify(client, message, user_id, is_second, is_third)
                    return
        else:
            usage = await mdb.check_and_increment_usage(user_id)
            if not usage["allowed"]:
                limits = await mdb.get_global_limits()
                await message.reply_text(
                    f"**🚫 Daily limit reached ({limits['free_limit']})**\n\n"
                    "Upgrade to Prime for unlimited access!"
                )
                return

    await mdb.async_db["file_links"].update_one(
        {"link_id": link_id}, {"$inc": {"access_count": 1}}
    )

    mins = DELETE_TIMER // 60
    usage_text = "🌟 Prime" if is_prime else "📊 Link Access"

    for idx, file_info in enumerate(files):
        file_type = file_info["type"]
        file_id = file_info["file_id"]
        original_caption = file_info.get("caption", "")
        is_last = idx == len(files) - 1

        if is_last:
            full_caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}</b>"
            if original_caption:
                full_caption += f"\n\n{original_caption}"

            USER_CURRENT_VIDEO[user_id] = file_id
            history = await mdb.get_watch_history(user_id, limit=2)
            has_previous = len(history) > 0

            buttons = []
            if has_previous:
                buttons.append([
                    InlineKeyboardButton("⬅️ Back", callback_data=f"prev_{user_id}"),
                    InlineKeyboardButton("🎬 Next", callback_data="getvideo"),
                ])
            else:
                buttons.append([InlineKeyboardButton("🎬 Next", callback_data="getvideo")])
            buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{user_id}")])
            markup = InlineKeyboardMarkup(buttons)

            try:
                if file_type == "video":
                    sent = await client.send_video(
                        message.chat.id, file_id, caption=full_caption,
                        protect_content=PROTECT_CONTENT, reply_markup=markup,
                    )
                elif file_type == "photo":
                    sent = await client.send_photo(
                        message.chat.id, file_id, caption=full_caption,
                        protect_content=PROTECT_CONTENT, reply_markup=markup,
                    )
                else:
                    sent = await client.send_document(
                        message.chat.id, file_id, caption=full_caption,
                        protect_content=PROTECT_CONTENT, reply_markup=markup,
                    )
                await mdb.add_to_watch_history(user_id, file_id, file_type)
                USER_ACTIVE_VIDEOS.setdefault(user_id, set()).add(sent.id)
                asyncio.create_task(auto_delete(client, message.chat.id, sent.id, user_id))
            except Exception as e:
                print(f"[handle_link_access] error sending last file: {e}")
        else:
            try:
                if file_type == "video":
                    await client.send_video(
                        message.chat.id, file_id, caption=original_caption,
                        protect_content=PROTECT_CONTENT,
                    )
                elif file_type == "photo":
                    await client.send_photo(
                        message.chat.id, file_id, caption=original_caption,
                        protect_content=PROTECT_CONTENT,
                    )
                else:
                    await client.send_document(
                        message.chat.id, file_id, caption=original_caption,
                        protect_content=PROTECT_CONTENT,
                    )
            except Exception as e:
                print(f"[handle_link_access] error sending file {idx}: {e}")
