from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from vars import ADMIN_ID, DELETE_TIMER, PROTECT_CONTENT, POST_CHANNEL
from Database.maindb import mdb
import string
import random
import os
import asyncio
import subprocess
import tempfile
from datetime import datetime

# Store temporary link generation sessions
LINK_SESSIONS = {}

# Store screenshot navigation sessions for admin
SCREENSHOT_SESSIONS = {}


def generate_link_id():
    """Generate a unique 8-character link ID"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))


# ==================== START LINK GENERATION ====================

@Client.on_message(filters.command("l") & filters.private & filters.user(ADMIN_ID))
async def start_link_generation(client: Client, message: Message):
    """Start the link generation process"""
    user_id = message.from_user.id

    # Initialize session
    LINK_SESSIONS[user_id] = {
        "files": [],
        "state": "collecting",
        "count_msg_id": None,
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
    (filters.video | filters.photo | filters.document)
)
async def collect_files(client: Client, message: Message):
    """Collect files from admin for link generation, or handle custom screenshot photo"""
    user_id = message.from_user.id

    # --- Handle custom screenshot upload ---
    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if ss_session and ss_session.get("state") == "awaiting_custom_photo":
        if message.photo:
            await _handle_custom_photo(client, message, user_id, ss_session)
        else:
            await message.reply_text("❌ Please send a **photo** for custom screenshot.")
        return

    # --- Handle normal file collection ---
    if user_id not in LINK_SESSIONS or LINK_SESSIONS[user_id]["state"] != "collecting":
        return

    session = LINK_SESSIONS[user_id]
    old_count_msg = session.get("count_msg_id")

    # Extract file info
    file_info = {}
    if message.video:
        file_info = {
            "type": "video",
            "file_id": message.video.file_id,
            "duration": message.video.duration or 0,
            "caption": message.caption or ""
        }
    elif message.photo:
        file_info = {
            "type": "photo",
            "file_id": message.photo.file_id,
            "caption": message.caption or ""
        }
    elif message.document:
        file_info = {
            "type": "document",
            "file_id": message.document.file_id,
            "file_name": getattr(message.document, "file_name", "file"),
            "mime_type": getattr(message.document, "mime_type", ""),
            "duration": 0,
            "caption": message.caption or ""
        }

    session["files"].append(file_info)

    # Delete old count message so new one appears after the latest file
    if old_count_msg:
        try:
            await client.delete_messages(message.chat.id, old_count_msg)
        except:
            pass

    # Send updated count message (always appears after latest file)
    new_msg = await message.reply_text(
        "**📁 Send files to generate link**\n\n"
        f"Files collected: **{len(session['files'])}**\n\n"
        "Use /m_link to generate screenshots & link"
    )
    session["count_msg_id"] = new_msg.id


# ==================== GENERATE SCREENSHOTS & LINK ====================

@Client.on_message(filters.command("m_link") & filters.private & filters.user(ADMIN_ID))
async def generate_multi_link(client: Client, message: Message):
    """Generate screenshots from collected files, show navigation to admin"""
    user_id = message.from_user.id

    if user_id not in LINK_SESSIONS:
        await message.reply_text("❌ No active session. Use /l to start.")
        return

    session = LINK_SESSIONS[user_id]
    if not session["files"]:
        await message.reply_text("❌ No files collected. Send files first.")
        return

    status_msg = await message.reply_text("⏳ **Generating screenshots from your files…**\n\nThis may take a moment.")

    used_timestamps = []
    try:
        screenshots = await generate_screenshots(client, message.chat.id, session["files"], used_timestamps)
    except Exception as e:
        await status_msg.edit_text(f"❌ Screenshot generation failed:\n`{e}`")
        return

    if not screenshots:
        await status_msg.edit_text("❌ Could not generate screenshots. Make sure files are videos.")
        return

    # Generate and store link in DB
    link_id = generate_link_id()
    await mdb.async_db["file_links"].insert_one({
        "link_id": link_id,
        "files": session["files"],
        "created_by": user_id,
        "created_at": datetime.now(),
        "access_count": 0
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
    }

    del LINK_SESSIONS[user_id]
    await status_msg.delete()

    await show_screenshot(client, message.chat.id, user_id, send_new=True)


# ==================== SHOW SCREENSHOT WITH NAVIGATION ====================

async def show_screenshot(client: Client, chat_id: int, user_id: int, send_new: bool = False):
    """Send or edit message showing current screenshot with navigation buttons."""
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
        "Use buttons below to browse, customise, or post to channel."
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
        ]
    ]
    markup = InlineKeyboardMarkup(buttons)
    nav_msg_id = ss_session.get("nav_msg_id")

    if send_new or not nav_msg_id:
        try:
            sent = await client.send_photo(chat_id, photo=photo_path, caption=caption, reply_markup=markup)
            ss_session["nav_msg_id"] = sent.id
        except Exception as e:
            print(f"show_screenshot send error: {e}")
    else:
        try:
            await client.edit_message_media(
                chat_id=chat_id,
                message_id=nav_msg_id,
                media=InputMediaPhoto(media=photo_path, caption=caption),
                reply_markup=markup
            )
        except Exception as e:
            print(f"show_screenshot edit error: {e}")
            # Fallback: delete and send new
            try:
                await client.delete_messages(chat_id, nav_msg_id)
            except:
                pass
            try:
                sent = await client.send_photo(chat_id, photo=photo_path, caption=caption, reply_markup=markup)
                ss_session["nav_msg_id"] = sent.id
            except Exception as e2:
                print(f"show_screenshot fallback error: {e2}")


# ==================== GENERATE SCREENSHOTS HELPER ====================

async def generate_screenshots(client: Client, chat_id: int, files: list,
                                used_timestamps: list = None, max_shots: int = 20) -> list:
    """
    Download video files and extract screenshots using ffmpeg.
    used_timestamps: list of (file_index, timestamp) tuples already used.
    Returns list of local file paths to the screenshots.
    """
    if used_timestamps is None:
        used_timestamps = []

    video_files = [(i, f) for i, f in enumerate(files) if f["type"] in ("video", "document")]

    if not video_files:
        return []

    # Calculate total duration for proportional distribution
    total_duration = sum(f.get("duration", 0) for _, f in video_files)

    screenshots_per_file = {}
    if total_duration > 0 and len(video_files) > 1:
        for orig_idx, f in video_files:
            dur = max(f.get("duration", 0), 1)
            share = dur / total_duration
            count = max(1, round(share * max_shots))
            screenshots_per_file[orig_idx] = count
    else:
        per = max(1, max_shots // len(video_files))
        for orig_idx, _ in video_files:
            screenshots_per_file[orig_idx] = per

    # Normalize to max_shots
    total_assigned = sum(screenshots_per_file.values())
    if total_assigned > max_shots:
        scale = max_shots / total_assigned
        for k in screenshots_per_file:
            screenshots_per_file[k] = max(1, round(screenshots_per_file[k] * scale))

    all_screenshots = []
    tmpdir = tempfile.mkdtemp(prefix="bot_ss_")

    for orig_idx, f in video_files:
        file_id = f["file_id"]
        want = screenshots_per_file.get(orig_idx, 1)

        try:
            dl_path = await client.download_media(
                file_id, file_name=os.path.join(tmpdir, f"video_{orig_idx}")
            )
        except Exception as e:
            print(f"Download error for file {orig_idx}: {e}")
            continue

        if not dl_path or not os.path.exists(dl_path):
            continue

        # Get actual duration via ffprobe
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", dl_path],
                capture_output=True, text=True, timeout=30
            )
            actual_dur = float(result.stdout.strip())
        except:
            actual_dur = max(f.get("duration", 30) or 30, 1)

        # Adjust: 1 screenshot per second of video, capped at want
        adj_want = min(want, max(1, int(actual_dur)))

        used_for_file = {ts for (fi, ts) in used_timestamps if fi == orig_idx}
        timestamps = _pick_timestamps(actual_dur, adj_want, used_for_file)

        if not timestamps:
            os.remove(dl_path)
            continue

        for ts in timestamps:
            out_path = os.path.join(tmpdir, f"ss_{orig_idx}_{ts:.2f}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-ss", str(ts), "-i", dl_path,
                     "-frames:v", "1", "-q:v", "2", out_path, "-y"],
                    capture_output=True, timeout=30
                )
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    all_screenshots.append(out_path)
                    used_timestamps.append((orig_idx, ts))
            except Exception as e:
                print(f"ffmpeg error at ts={ts}: {e}")

        try:
            os.remove(dl_path)
        except:
            pass

    return all_screenshots


def _pick_timestamps(duration: float, count: int, exclude: set = None) -> list:
    """Pick evenly-spaced timestamps, avoiding already-used ones."""
    if duration <= 0:
        return []
    exclude = exclude or set()
    start = duration * 0.05
    end = duration * 0.95
    if end <= start:
        start = 0.0
        end = duration

    # Build candidate pool (4x density)
    pool_size = max(count * 4, 20)
    step = (end - start) / pool_size
    candidates = []
    t = start
    while t <= end:
        rounded = round(t, 2)
        if not any(abs(rounded - ex) < 1.0 for ex in exclude):
            candidates.append(rounded)
        t += step

    if not candidates:
        return []
    if len(candidates) <= count:
        return candidates
    # Evenly sample
    if count == 1:
        return [candidates[len(candidates) // 2]]
    indices = [round(i * (len(candidates) - 1) / (count - 1)) for i in range(count)]
    return [candidates[i] for i in sorted(set(indices))]


# ==================== CUSTOM PHOTO HANDLER ====================

async def _handle_custom_photo(client: Client, message: Message, user_id: int, ss_session: dict):
    """Admin sent a custom photo for the screenshot slot."""
    photo = message.photo
    tmpdir = tempfile.mkdtemp(prefix="bot_custom_")
    photo_path = os.path.join(tmpdir, "custom_photo.jpg")

    try:
        await client.download_media(photo.file_id, file_name=photo_path)
    except Exception as e:
        await message.reply_text(f"❌ Failed to download photo: {e}")
        return

    # Insert custom photo at current position
    idx = ss_session["current_index"]
    ss_session["screenshots"].insert(idx, photo_path)
    ss_session["state"] = "browsing"

    try:
        await message.reply_text("✅ Custom photo saved! Showing it now…")
    except:
        pass

    await show_screenshot(client, message.chat.id, user_id, send_new=True)


# ==================== POST TO CHANNEL ====================

async def post_screenshot_to_channel(client: Client, chat_id: int, user_id: int):
    """Send current screenshot to POST_CHANNEL with a Generate Link button."""
    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if not ss_session:
        await client.send_message(chat_id, "❌ No active screenshot session.")
        return

    if not POST_CHANNEL:
        await client.send_message(chat_id, "❌ `POST_CHANNEL` is not configured in `vars.py`.")
        return

    idx = ss_session["current_index"]
    photo_path = ss_session["screenshots"][idx]
    link = ss_session["link"]

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Generate Link", url=link)
    ]])

    try:
        await client.send_photo(
            POST_CHANNEL,
            photo=photo_path,
            caption="📥 **Click the button below to get the files!**",
            reply_markup=markup
        )
        await client.send_message(chat_id, "✅ Screenshot **posted to channel** with Generate Link button!")
    except Exception as e:
        await client.send_message(chat_id, f"❌ Failed to post to channel:\n`{e}`")


# ==================== HANDLE LINK ACCESS ====================

async def handle_link_access(client: Client, message: Message, link_id: str):
    """Handle when user accesses a generated link"""
    user_id = message.from_user.id

    link_data = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not link_data:
        await message.reply_text("❌ Invalid or expired link.")
        return

    files = link_data["files"]
    if not files:
        await message.reply_text("❌ No files found in this link.")
        return

    from .cmds import get_cached_user_data, get_cached_verification, show_verify, USER_ACTIVE_VIDEOS, auto_delete, USER_CURRENT_VIDEO
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

        if idx == len(files) - 1:
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
                    InlineKeyboardButton("🎬 Next", callback_data="getvideo")
                ])
            else:
                buttons.append([InlineKeyboardButton("🎬 Next", callback_data="getvideo")])
            buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{user_id}")])

            try:
                if file_type == "video":
                    sent = await client.send_video(message.chat.id, file_id, caption=full_caption, protect_content=PROTECT_CONTENT, reply_markup=InlineKeyboardMarkup(buttons))
                elif file_type == "photo":
                    sent = await client.send_photo(message.chat.id, file_id, caption=full_caption, protect_content=PROTECT_CONTENT, reply_markup=InlineKeyboardMarkup(buttons))
                elif file_type == "document":
                    sent = await client.send_document(message.chat.id, file_id, caption=full_caption, protect_content=PROTECT_CONTENT, reply_markup=InlineKeyboardMarkup(buttons))

                await mdb.add_to_watch_history(user_id, file_id, file_type)
                USER_ACTIVE_VIDEOS.setdefault(user_id, set()).add(sent.id)
                asyncio.create_task(auto_delete(client, message.chat.id, sent.id, user_id))
            except Exception as e:
                print(f"Error sending last file: {e}")
        else:
            try:
                if file_type == "video":
                    await client.send_video(message.chat.id, file_id, caption=original_caption, protect_content=PROTECT_CONTENT)
                elif file_type == "photo":
                    await client.send_photo(message.chat.id, file_id, caption=original_caption, protect_content=PROTECT_CONTENT)
                elif file_type == "document":
                    await client.send_document(message.chat.id, file_id, caption=original_caption, protect_content=PROTECT_CONTENT)
            except Exception as e:
                print(f"Error sending file: {e}")
