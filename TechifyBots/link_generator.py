from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from vars import ADMIN_ID, DELETE_TIMER, PROTECT_CONTENT, POST_CHANNEL, IS_FSUB, IS_VERIFY
from Database.maindb import mdb
from Database.userdb import udb
from .cmds import *
import string
import random
import os
import asyncio
import tempfile, shutil
from datetime import datetime

LINK_SESSIONS = {}
SCREENSHOT_SESSIONS = {}
_COLLECT_LOCKS = {}
SS_CANCEL_FLAGS = {}
SS_BG_TASKS = {}
SS_DL_CUSTOM_ACTIVE = {}
_SS_PHOTO_CACHE = {}
_FFMPEG_SEM = asyncio.Semaphore(2)


def generate_link_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

def generate_group_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=12))

def _get_collect_lock(user_id):
    if user_id not in _COLLECT_LOCKS:
        _COLLECT_LOCKS[user_id] = asyncio.Lock()
    return _COLLECT_LOCKS[user_id]

def _cleanup_ss_files(screenshots):
    temp_dirs = set()
    for path in screenshots:
        try:
            if path and os.path.exists(path):
                temp_dirs.add(os.path.dirname(path))
        except Exception:
            pass
        _SS_PHOTO_CACHE.pop(path, None)
    for d in temp_dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

def _build_progress_markup(user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎨 Custom", callback_data=f"ss_dl_custom_{user_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"ss_dl_cancel_{user_id}"),
    ]])

def _clear_link_session(user_id):
    LINK_SESSIONS.pop(user_id, None)
    SS_CANCEL_FLAGS.pop(user_id, None)
    SS_BG_TASKS.pop(user_id, None)
    SS_DL_CUSTOM_ACTIVE.pop(user_id, None)


@Client.on_message(filters.command("l") & filters.private & filters.user(ADMIN_ID))
async def start_link_generation(client: Client, message: Message):
    user_id = message.from_user.id
    LINK_SESSIONS.pop(user_id, None)
    SS_CANCEL_FLAGS.pop(user_id, None)
    SS_BG_TASKS.pop(user_id, None)
    SS_DL_CUSTOM_ACTIVE.pop(user_id, None)
    LINK_SESSIONS[user_id] = {
        "files": [], "state": "collecting",
        "count_msg_id": None, "count_chat_id": message.chat.id, "file_msg_ids": [],
    }
    sent = await message.reply_text(
        "**📁 Send files to generate link**\n\nFiles collected: **0**\n\n"
        "Supported: Photo, Video, Audio, Document\nSend /m_link when done."
    )
    LINK_SESSIONS[user_id]["count_msg_id"] = sent.id


@Client.on_message(
    filters.private & filters.user(ADMIN_ID) &
    (filters.video | filters.photo | filters.document | filters.animation |
     filters.audio | filters.voice)
)
async def collect_files(client: Client, message: Message):
    user_id = message.from_user.id

    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if ss_session and ss_session.get("state") == "awaiting_custom_photo":
        if message.photo or message.video:
            await _handle_custom_media(client, message, user_id, ss_session)
        else:
            await message.reply_text("❌ Please send a **photo or video** for the custom screenshot.")
        return

    if SS_DL_CUSTOM_ACTIVE.get(user_id):
        if message.photo or message.video:
            await _handle_dl_custom_media(client, message, user_id)
        else:
            await message.reply_text("❌ Please send a **photo or video** for the channel post.")
        return

    if user_id not in LINK_SESSIONS or LINK_SESSIONS[user_id]["state"] != "collecting":
        return

    lock = _get_collect_lock(user_id)
    async with lock:
        if user_id not in LINK_SESSIONS or LINK_SESSIONS[user_id]["state"] != "collecting":
            return
        session = LINK_SESSIONS[user_id]

        file_info = None
        if message.animation:
            file_info = {"type": "video", "file_id": message.animation.file_id,
                         "duration": message.animation.duration or 0, "caption": message.caption or ""}
        elif message.video:
            file_info = {"type": "video", "file_id": message.video.file_id,
                         "duration": message.video.duration or 0, "caption": message.caption or ""}
        elif message.photo:
            file_info = {"type": "photo", "file_id": message.photo.file_id,
                         "duration": 0, "caption": message.caption or ""}
        elif message.audio:
            file_info = {"type": "audio", "file_id": message.audio.file_id,
                         "duration": getattr(message.audio, "duration", 0) or 0,
                         "file_name": getattr(message.audio, "file_name", "audio.mp3") or "audio.mp3",
                         "caption": message.caption or ""}
        elif message.voice:
            file_info = {"type": "voice", "file_id": message.voice.file_id,
                         "duration": getattr(message.voice, "duration", 0) or 0,
                         "caption": message.caption or ""}
        elif message.document:
            mime = getattr(message.document, "mime_type", "") or ""
            file_info = {"type": "document", "file_id": message.document.file_id,
                         "file_name": getattr(message.document, "file_name", "file") or "file",
                         "mime_type": mime, "duration": 0, "caption": message.caption or ""}

        if file_info is None:
            return

        session["files"].append(file_info)
        session.setdefault("file_msg_ids", []).append(message.id)
        count = len(session["files"])
        chat_id = session["count_chat_id"]
        old_msg_id = session.get("count_msg_id")

        # Always delete old count msg and send new one AFTER the file
        if old_msg_id:
            try:
                await client.delete_messages(chat_id, old_msg_id)
            except Exception:
                pass

        new_msg = await message.reply_text(
            "**📁 Send files to generate link**\n\n"
            f"Files collected: **{count}**\n\n"
            "Supported: Photo, Video, Audio, Document\nSend /m_link when done."
        )
        session["count_msg_id"] = new_msg.id


@Client.on_message(filters.command("m_link") & filters.private & filters.user(ADMIN_ID))
async def generate_multi_link(client: Client, message: Message):
    user_id = message.from_user.id

    if user_id not in LINK_SESSIONS:
        await message.reply_text("❌ No active session. Use /l to start.")
        return
    session = LINK_SESSIONS[user_id]
    if not session["files"]:
        await message.reply_text("❌ No files collected. Send files first.")
        return

    for mid in session.get("file_msg_ids", []):
        try:
            await client.delete_messages(message.chat.id, mid)
        except Exception:
            pass

    count_msg_id = session.get("count_msg_id")
    if count_msg_id:
        try:
            await client.delete_messages(session["count_chat_id"], count_msg_id)
        except Exception:
            pass

    SS_CANCEL_FLAGS[user_id] = False
    SS_DL_CUSTOM_ACTIVE[user_id] = False
    session["bg_generating"] = True

    status_msg = await message.reply_text(
        "⏳ **Generating Screenshots…**\n\n📊 Starting…",
        reply_markup=_build_progress_markup(user_id)
    )
    session["status_msg_id"] = status_msg.id
    session["status_chat_id"] = message.chat.id

    task = asyncio.create_task(
        _run_screenshot_generation(client, message, user_id, session, status_msg)
    )
    SS_BG_TASKS[user_id] = task


async def _run_screenshot_generation(client, message, user_id, session, status_msg):
    used_timestamps = []
    chat_id = message.chat.id

    try:
        screenshots = await generate_screenshots(
            client, session["files"], used_timestamps,
            status_msg=status_msg, cancel_flag=SS_CANCEL_FLAGS, cancel_key=user_id
        )
    except asyncio.CancelledError:
        screenshots = []
    except Exception as e:
        screenshots = []
        if not SS_CANCEL_FLAGS.get(user_id):
            try:
                await status_msg.edit_text(f"❌ Failed:\n`{e}`", reply_markup=None)
            except Exception:
                pass
            _clear_link_session(user_id)
            return

    session["bg_generating"] = False

    if SS_CANCEL_FLAGS.get(user_id):
        _cleanup_ss_files(screenshots)
        try:
            await status_msg.edit_text("❌ Screenshot generation cancelled.", reply_markup=None)
        except Exception:
            pass
        _clear_link_session(user_id)
        return

    # Admin clicked Custom during download — store results and wait
    if SS_DL_CUSTOM_ACTIVE.get(user_id):
        session["completed_screenshots"] = screenshots
        session["completed_used_timestamps"] = used_timestamps
        return

    if not screenshots:
        try:
            await status_msg.edit_text(
                "❌ Could not generate screenshots.\n\nMake sure files are **video files**.",
                reply_markup=None
            )
        except Exception:
            pass
        _clear_link_session(user_id)
        return

    await _finish_and_show_navigator(
        client, chat_id, user_id, session, status_msg, screenshots, used_timestamps
    )


async def _finish_and_show_navigator(client, chat_id, user_id, session, status_msg, screenshots, used_timestamps):
    files = session["files"]
    if len(files) == 1:
        post_id = files[0]["file_id"]
        is_group = False
    else:
        post_id = generate_group_id()
        is_group = True

    link_id = generate_link_id()
    await mdb.async_db["file_links"].insert_one({
        "link_id": link_id, "post_id": post_id, "is_group": is_group,
        "files": files, "created_by": user_id,
        "created_at": datetime.now(), "access_count": 0,
    })
    bot_info = await client.get_me()
    link = f"https://t.me/{bot_info.username}?start=link_{link_id}"

    for path in screenshots:
        if path not in _SS_PHOTO_CACHE:
            _SS_PHOTO_CACHE[path] = {"bytes": None, "tg_file_id": None}
            try:
                with open(path, "rb") as f:
                    _SS_PHOTO_CACHE[path]["bytes"] = f.read()
            except Exception:
                pass

    SCREENSHOT_SESSIONS[user_id] = {
        "screenshots": screenshots, "used_timestamps": used_timestamps,
        "current_index": 0, "link": link, "link_id": link_id,
        "post_id": post_id, "is_group": is_group, "source_files": files,
        "state": "browsing", "nav_msg_id": None, "nav_chat_id": chat_id,
    }

    _clear_link_session(user_id)
    try:
        await status_msg.delete()
    except Exception:
        pass
    await show_screenshot(client, chat_id, user_id, send_new=True)


async def show_screenshot(client, chat_id, user_id, send_new=False):
    ss_session = SCREENSHOT_SESSIONS.get(user_id)
    if not ss_session:
        return

    idx = ss_session["current_index"]
    screenshots = ss_session["screenshots"]
    total = len(screenshots)
    link = ss_session["link"]
    post_id = ss_session.get("post_id", ss_session.get("link_id", ""))
    photo_path = screenshots[idx]

    caption = (
        f"🖼 **Screenshot {idx + 1} of {total}**\n\n"
        f"🔗 Link: `{link}`\n\n"
        f"🆔 Post ID: `{post_id}`"
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
        [InlineKeyboardButton("📤 Send to Channel", callback_data=f"ss_send_{user_id}")],
        [InlineKeyboardButton("❌ Cancel Post", callback_data=f"ss_cancel_post_{user_id}")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    nav_msg_id = ss_session.get("nav_msg_id")

    if photo_path not in _SS_PHOTO_CACHE:
        _SS_PHOTO_CACHE[photo_path] = {"bytes": None, "tg_file_id": None}
        try:
            with open(photo_path, "rb") as f:
                _SS_PHOTO_CACHE[photo_path]["bytes"] = f.read()
        except Exception:
            pass

    cache = _SS_PHOTO_CACHE[photo_path]
    # Prefer tg_file_id (instant, no re-upload), then bytes, then path
    if cache.get("tg_file_id"):
        photo_src = cache["tg_file_id"]
    elif cache.get("bytes"):
        photo_src = cache["bytes"]
    else:
        photo_src = photo_path

    if not send_new and nav_msg_id:
        try:
            await client.edit_message_media(
                chat_id=chat_id, message_id=nav_msg_id,
                media=InputMediaPhoto(media=photo_src, caption=caption),
                reply_markup=markup,
            )
            return
        except Exception:
            try:
                await client.delete_messages(chat_id, nav_msg_id)
            except Exception:
                pass
            ss_session["nav_msg_id"] = None

    try:
        sent = await client.send_photo(chat_id, photo=photo_src, caption=caption, reply_markup=markup)
        ss_session["nav_msg_id"] = sent.id
        # Cache Telegram file_id so all future edits are instant (no re-upload)
        if sent.photo and not cache.get("tg_file_id"):
            cache["tg_file_id"] = sent.photo.file_id
    except Exception as e:
        print(f"[show_screenshot] error: {e}")


async def _run_async(cmd, timeout=120):
    async with _FFMPEG_SEM:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return -1, "", "timeout"
        except Exception as e:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return -1, "", str(e)


async def generate_screenshots(
    client, files, used_timestamps=None, max_shots=20,
    status_msg=None, cancel_flag=None, cancel_key=None,
):
    if used_timestamps is None:
        used_timestamps = []

    def is_cancelled():
        return cancel_flag and cancel_key is not None and cancel_flag.get(cancel_key, False)

    video_files = [(i, f) for i, f in enumerate(files) if f["type"] in ("video", "document")]
    if not video_files:
        return []

    total_meta_dur = sum(max(f.get("duration", 0), 0) for _, f in video_files)
    screenshots_per_file = {}
    if total_meta_dur > 0:
        for orig_idx, f in video_files:
            dur = max(f.get("duration", 0), 1)
            screenshots_per_file[orig_idx] = max(1, round((dur / total_meta_dur) * max_shots))
    else:
        per = max(1, max_shots // len(video_files))
        for orig_idx, _ in video_files:
            screenshots_per_file[orig_idx] = per

    total_assigned = sum(screenshots_per_file.values())
    if total_assigned > max_shots:
        scale = max_shots / total_assigned
        for k in screenshots_per_file:
            screenshots_per_file[k] = max(1, round(screenshots_per_file[k] * scale))

    all_screenshots = []
    tmpdir = tempfile.mkdtemp(prefix="bot_ss_")

    for file_number, (orig_idx, f) in enumerate(video_files, start=1):
        if is_cancelled():
            break

        file_id = f["file_id"]
        want = screenshots_per_file.get(orig_idx, 1)

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ **Generating Screenshots…**\n\n"
                    f"📥 Downloading file {file_number}/{len(video_files)}…",
                    reply_markup=_build_progress_markup(cancel_key)
                )
            except Exception:
                pass

        if is_cancelled():
            break

        dl_path = None
        try:
            fname = f.get("file_name") or f"video_{orig_idx}.mp4"
            if "." not in os.path.basename(fname):
                fname += ".mp4"
            dest = os.path.join(tmpdir, f"file_{orig_idx}_{fname}")
            dl_path = await client.download_media(file_id, file_name=dest)
        except Exception as e:
            print(f"[generate_screenshots] download error file {orig_idx}: {e}")
            continue

        if is_cancelled():
            try:
                os.remove(dl_path)
            except Exception:
                pass
            break

        if not dl_path or not os.path.exists(dl_path):
            continue

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ **Generating Screenshots…**\n\n"
                    f"🔍 Probing file {file_number}/{len(video_files)}…",
                    reply_markup=_build_progress_markup(cancel_key)
                )
            except Exception:
                pass

        actual_dur = 0.0
        rc, stdout, _ = await _run_async([
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", dl_path,
        ], timeout=60)
        if rc == 0 and stdout.strip():
            try:
                actual_dur = float(stdout.strip())
            except ValueError:
                pass

        if actual_dur <= 0:
            actual_dur = max(f.get("duration", 0) or 0, 0)

        if actual_dur <= 0:
            try:
                os.remove(dl_path)
            except Exception:
                pass
            continue

        adj_want = min(want, max(1, int(actual_dur)))
        used_for_file = {ts for (fi, ts) in used_timestamps if fi == orig_idx}
        timestamps = _pick_random_timestamps(actual_dur, adj_want, used_for_file)

        if not timestamps:
            try:
                os.remove(dl_path)
            except Exception:
                pass
            continue

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ **Generating Screenshots…**\n\n"
                    f"🎬 Extracting {len(timestamps)} frames from file {file_number}/{len(video_files)}…",
                    reply_markup=_build_progress_markup(cancel_key)
                )
            except Exception:
                pass

        if is_cancelled():
            try:
                os.remove(dl_path)
            except Exception:
                pass
            break

        # Sequential extraction (semaphore inside _run_async prevents OOM)
        for ts in timestamps:
            if is_cancelled():
                break
            out_path = os.path.join(tmpdir, f"ss_{orig_idx}_{ts:.3f}.jpg")
            rc2, _, err2 = await _run_async([
                "ffmpeg", "-ss", f"{ts:.3f}", "-i", dl_path,
                "-frames:v", "1", "-q:v", "2", "-f", "image2", out_path, "-y",
            ], timeout=60)
            if rc2 == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                all_screenshots.append(out_path)
                used_timestamps.append((orig_idx, ts))
            else:
                print(f"[extract_frame] failed rc={rc2} ts={ts:.3f}: {err2[-200:]}")

        try:
            os.remove(dl_path)
        except Exception:
            pass

    return all_screenshots


def _pick_random_timestamps(duration, count, exclude=None):
    if duration <= 0 or count <= 0:
        return []
    exclude = exclude or set()
    start = duration * 0.05
    end = duration * 0.95
    if end <= start:
        start, end = 0.0, duration

    picked = []
    for _ in range(count * 20):
        if len(picked) >= count:
            break
        ts = round(random.uniform(start, end), 3)
        if any(abs(ts - ex) < 1.0 for ex in exclude):
            continue
        if any(abs(ts - p) < 1.0 for p in picked):
            continue
        picked.append(ts)

    if len(picked) < count:
        step = (end - start) / max(count * 2, 20)
        t = start
        while len(picked) < count and t <= end:
            ts = round(t, 3)
            if (not any(abs(ts - ex) < 1.0 for ex in exclude) and
                    not any(abs(ts - p) < 1.0 for p in picked)):
                picked.append(ts)
            t += step

    return sorted(picked)


async def _handle_custom_media(client, message, user_id, ss_session):
    """Admin sent photo/video during SS preview phase — insert at current position."""
    tmpdir = tempfile.mkdtemp(prefix="bot_custom_")
    media_path = None

    try:
        if message.photo:
            dest = os.path.join(tmpdir, "custom_photo.jpg")
            await client.download_media(message.photo.file_id, file_name=dest)
            media_path = dest
        elif message.video:
            vid_path = os.path.join(tmpdir, "custom_video.mp4")
            dl = await client.download_media(message.video.file_id, file_name=vid_path)
            rc, stdout, _ = await _run_async([
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", dl
            ], timeout=60)
            dur = 0.0
            if rc == 0 and stdout.strip():
                try:
                    dur = float(stdout.strip())
                except Exception:
                    pass
            ts = max(dur * 0.05, 0.5)
            frame_path = os.path.join(tmpdir, "frame.jpg")
            rc2, _, _ = await _run_async([
                "ffmpeg", "-ss", f"{ts:.3f}", "-i", dl,
                "-frames:v", "1", "-q:v", "2", "-f", "image2", frame_path, "-y"
            ], timeout=60)
            if rc2 != 0 or not os.path.exists(frame_path):
                await message.reply_text("❌ Could not extract frame. Try sending a photo.")
                shutil.rmtree(tmpdir, ignore_errors=True)
                return
            media_path = frame_path
    except Exception as e:
        await message.reply_text(f"❌ Failed: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return

    if not media_path or not os.path.exists(media_path) or os.path.getsize(media_path) == 0:
        await message.reply_text("❌ Downloaded file is empty.")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return

    idx = ss_session["current_index"]
    ss_session["screenshots"].insert(idx, media_path)
    ss_session["state"] = "browsing"
    _SS_PHOTO_CACHE[media_path] = {"bytes": None, "tg_file_id": None}
    try:
        with open(media_path, "rb") as fh:
            _SS_PHOTO_CACHE[media_path]["bytes"] = fh.read()
    except Exception:
        pass

    ask_msg_id = ss_session.pop("custom_ask_msg_id", None)
    ask_chat_id = ss_session.pop("custom_ask_chat_id", None)
    if ask_msg_id and ask_chat_id:
        try:
            await client.delete_messages(ask_chat_id, ask_msg_id)
        except Exception:
            pass

    chat_id = ss_session.get("nav_chat_id", message.chat.id)
    await show_screenshot(client, chat_id, user_id)
    try:
        await message.delete()
    except Exception:
        pass


async def _handle_dl_custom_media(client, message, user_id):
    """Admin sent photo/video WHILE SS generation runs in background.
    Cancel background, post custom file to channel, clean up.
    """
    # Cancel background task
    SS_CANCEL_FLAGS[user_id] = True
    task = SS_BG_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except Exception:
            pass

    SS_DL_CUSTOM_ACTIVE[user_id] = False

    link_session = LINK_SESSIONS.get(user_id)
    chat_id = message.chat.id

    # Edit the existing status message
    status_msg_id = link_session.get("status_msg_id") if link_session else None
    status_chat_id = link_session.get("status_chat_id") if link_session else None
    status_obj = None
    if status_msg_id and status_chat_id:
        try:
            status_obj = await client.get_messages(status_chat_id, status_msg_id)
        except Exception:
            pass

    if status_obj:
        try:
            await status_obj.edit_text("⏳ Processing your media for post…", reply_markup=None)
        except Exception:
            status_obj = None

    if not status_obj:
        status_obj = await message.reply_text("⏳ Processing your media for post…")

    tmpdir = tempfile.mkdtemp(prefix="bot_dl_custom_")
    media_path = None

    try:
        if message.photo:
            dest = os.path.join(tmpdir, "custom_photo.jpg")
            await client.download_media(message.photo.file_id, file_name=dest)
            media_path = dest
        elif message.video:
            vid_path = os.path.join(tmpdir, "custom_video.mp4")
            dl = await client.download_media(message.video.file_id, file_name=vid_path)
            rc, stdout, _ = await _run_async([
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", dl
            ], timeout=60)
            dur = 0.0
            if rc == 0 and stdout.strip():
                try:
                    dur = float(stdout.strip())
                except Exception:
                    pass
            ts = max(dur * 0.05, 0.5)
            frame_path = os.path.join(tmpdir, "frame.jpg")
            rc2, _, _ = await _run_async([
                "ffmpeg", "-ss", f"{ts:.3f}", "-i", dl,
                "-frames:v", "1", "-q:v", "2", "-f", "image2", frame_path, "-y"
            ], timeout=60)
            if rc2 != 0 or not os.path.exists(frame_path):
                try:
                    await status_obj.edit_text("❌ Could not extract frame.", reply_markup=None)
                except Exception:
                    pass
                shutil.rmtree(tmpdir, ignore_errors=True)
                return
            media_path = frame_path
    except Exception as e:
        try:
            await status_obj.edit_text(f"❌ Failed: {e}", reply_markup=None)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        return

    if not link_session:
        try:
            await status_obj.edit_text("❌ Session expired. Use /l to start.", reply_markup=None)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        return

    files = link_session["files"]
    if len(files) == 1:
        post_id = files[0]["file_id"]
        is_group = False
    else:
        post_id = generate_group_id()
        is_group = True

    link_id = generate_link_id()
    await mdb.async_db["file_links"].insert_one({
        "link_id": link_id, "post_id": post_id, "is_group": is_group,
        "files": files, "created_by": user_id,
        "created_at": datetime.now(), "access_count": 0,
    })
    bot_info = await client.get_me()
    link = f"https://t.me/{bot_info.username}?start=link_{link_id}"

    if not POST_CHANNEL:
        try:
            await status_obj.edit_text("❌ POST_CHANNEL not configured.", reply_markup=None)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        _clear_link_session(user_id)
        return

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Get Files", url=link)]])
    caption = (
        f"✨ **Here is your link** 👇\n\n"
        f"<blockquote>🆔 Post ID: <code>{post_id}</code></blockquote>"
    )

    try:
        await client.send_photo(POST_CHANNEL, photo=media_path, caption=caption, reply_markup=markup)
        try:
            await status_obj.edit_text("✅ Posted to channel!", reply_markup=None)
        except Exception:
            pass
    except Exception as e:
        try:
            await status_obj.edit_text(f"❌ Failed to post: `{e}`", reply_markup=None)
        except Exception:
            pass

    shutil.rmtree(tmpdir, ignore_errors=True)
    _clear_link_session(user_id)
    try:
        await message.delete()
    except Exception:
        pass


async def post_screenshot_to_channel(client, chat_id, user_id, query=None):
    """Post current screenshot to channel. Delete temp files but keep DB."""
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
    post_id = ss_session.get("post_id", ss_session.get("link_id", ""))

    cache = _SS_PHOTO_CACHE.get(photo_path, {})
    if cache.get("tg_file_id"):
        photo_src = cache["tg_file_id"]
    elif cache.get("bytes"):
        photo_src = cache["bytes"]
    else:
        photo_src = photo_path

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Get Files", url=link)]])
    caption = (
        f"✨ **Here is your link** 👇\n\n"
        f"<blockquote>🆔 Post ID: <code>{post_id}</code></blockquote>"
    )

    try:
        await client.send_photo(POST_CHANNEL, photo=photo_src, caption=caption, reply_markup=markup)

        if query:
            await query.answer("✅ Posted to channel!", show_alert=True)
        else:
            await client.send_message(chat_id, "✅ Posted to channel!")

        nav_msg_id = ss_session.get("nav_msg_id")
        nav_chat_id = ss_session.get("nav_chat_id")
        if nav_msg_id:
            try:
                await client.delete_messages(nav_chat_id, nav_msg_id)
            except Exception:
                pass

        # Delete temp SS files — DB record stays so link keeps working
        _cleanup_ss_files(ss_session.get("screenshots", []))
        SCREENSHOT_SESSIONS.pop(user_id, None)

    except Exception as e:
        err = f"❌ Failed to post: `{e}`"
        if query:
            await query.answer(err[:200], show_alert=True)
        else:
            await client.send_message(chat_id, err)


async def handle_link_access(client: Client, message: Message, link_id: str):
    user_id = message.from_user.id

    link_data = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not link_data:
        await message.reply_text("❌ Invalid or expired link.")
        return

    files = link_data.get("files", [])
    if not files:
        await message.reply_text("❌ No files found in this link.")
        return

    if await udb.is_user_banned(user_id):
        await message.reply_text("**🚫 You are banned**")
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

    await mdb.async_db["file_links"].update_one({"link_id": link_id}, {"$inc": {"access_count": 1}})

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
                        protect_content=PROTECT_CONTENT, reply_markup=markup)
                elif file_type == "photo":
                    sent = await client.send_photo(
                        message.chat.id, file_id, caption=full_caption,
                        protect_content=PROTECT_CONTENT, reply_markup=markup)
                elif file_type in ("audio", "voice"):
                    sent = await client.send_audio(
                        message.chat.id, file_id, caption=full_caption,
                        protect_content=PROTECT_CONTENT, reply_markup=markup)
                else:
                    sent = await client.send_document(
                        message.chat.id, file_id, caption=full_caption,
                        protect_content=PROTECT_CONTENT, reply_markup=markup)
                await mdb.add_to_watch_history(user_id, file_id, file_type)
                USER_ACTIVE_VIDEOS.setdefault(user_id, set()).add(sent.id)
                asyncio.create_task(auto_delete(client, message.chat.id, sent.id, user_id))
            except Exception as e:
                print(f"[handle_link_access] error sending last file: {e}")
        else:
            try:
                if file_type == "video":
                    await client.send_video(message.chat.id, file_id, caption=original_caption,
                                            protect_content=PROTECT_CONTENT)
                elif file_type == "photo":
                    await client.send_photo(message.chat.id, file_id, caption=original_caption,
                                            protect_content=PROTECT_CONTENT)
                elif file_type in ("audio", "voice"):
                    await client.send_audio(message.chat.id, file_id, caption=original_caption,
                                            protect_content=PROTECT_CONTENT)
                else:
                    await client.send_document(message.chat.id, file_id, caption=original_caption,
                                               protect_content=PROTECT_CONTENT)
            except Exception as e:
                print(f"[handle_link_access] error sending file {idx}: {e}")
