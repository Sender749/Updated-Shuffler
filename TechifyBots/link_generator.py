"""
link_generator.py — Admin /l and /m_link flow
===============================================

/l  → bot asks admin to send files (any type). Shows live file count.
      Admin can send /m_link at any time to proceed to link generation.

/m_link → bot deletes collected files from session, generates screenshots
          in background, and shows progress with Cancel / Custom buttons.

Cancel  → stops everything, cleans up.
Custom  → asks admin to send a custom thumbnail/file.  Bot posts that
          file to POST_CHANNEL with the generated link in caption.
Back    → returns to SS preview (or live progress if still running).

After SS generation, bot shows SS navigator with:
  ⬅️ <n/total> ➡️  |  🖼 Custom  |  ♻️ More  |  📤 Post  |  ❌ Cancel
"""

from __future__ import annotations
import asyncio, os, random, string, time
from datetime import datetime
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo,
)
from pyrogram.errors import MessageNotModified

from vars import ADMIN_IDS, POST_CHANNEL
from Database.maindb import mdb

# ── helpers ──────────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def _rand_id(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


# ── per-admin session state ───────────────────────────────────────────────────
#
# LINK_SESSIONS[admin_id] = {
#   "files": [...],           # list of {file_id, media_type, msg_id}
#   "ask_msg_id": int,        # message id of the "send files" prompt
#   "state": str,             # "collecting" | "ss_progress" | "ss_done" | "custom_wait"
#   "ss_list": [...],         # generated screenshot file_ids
#   "ss_index": int,          # currently shown SS index
#   "nav_msg_id": int,        # message being edited for SS navigation
#   "cancel_flag": bool,
#   "bg_task": asyncio.Task,
#   "chat_id": int,
# }

LINK_SESSIONS: dict[int, dict] = {}

# kept for callback.py compatibility (it imports these names)
SCREENSHOT_SESSIONS: dict = LINK_SESSIONS
SS_CANCEL_FLAGS: dict = {}
SS_BG_TASKS: dict = {}
SS_DL_CUSTOM_ACTIVE: dict = {}


# ── /l command ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("l") & filters.private)
async def cmd_l(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    # Reset any existing session
    _cancel_session(uid)
    LINK_SESSIONS[uid] = {
        "files": [],
        "ask_msg_id": None,
        "state": "collecting",
        "ss_list": [],
        "ss_index": 0,
        "nav_msg_id": None,
        "cancel_flag": False,
        "bg_task": None,
        "chat_id": message.chat.id,
    }
    ask = await message.reply_text(
        _ask_text(0),
        reply_markup=_ask_markup(),
    )
    LINK_SESSIONS[uid]["ask_msg_id"] = ask.id


# ── media collector ───────────────────────────────────────────────────────────

def _is_collecting(_, __, msg: Message) -> bool:
    if not msg.from_user:
        return False
    uid = msg.from_user.id
    sess = LINK_SESSIONS.get(uid)
    return bool(
        is_admin(uid)
        and sess
        and sess["state"] == "collecting"
        and (msg.video or msg.photo or msg.document or msg.audio or msg.voice or msg.animation)
    )

collecting_filter = filters.create(_is_collecting)

@Client.on_message(collecting_filter & filters.private)
async def collect_file(client: Client, message: Message):
    uid = message.from_user.id
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    # Extract file info
    media, mtype = None, None
    if message.video:       media, mtype = message.video,     "video"
    elif message.photo:     media, mtype = message.photo,     "photo"
    elif message.document:  media, mtype = message.document,  "document"
    elif message.audio:     media, mtype = message.audio,     "audio"
    elif message.voice:     media, mtype = message.voice,     "voice"
    elif message.animation: media, mtype = message.animation, "animation"
    if not media:
        return

    sess["files"].append({
        "file_id": media.file_id,
        "media_type": mtype,
        "msg_id": message.id,
    })
    count = len(sess["files"])

    # Delete old ask message, send fresh one with correct count
    try:
        await client.delete_messages(message.chat.id, sess["ask_msg_id"])
    except Exception:
        pass
    ask = await message.reply_text(
        _ask_text(count),
        reply_markup=_ask_markup(),
    )
    sess["ask_msg_id"] = ask.id


def _ask_text(count: int) -> str:
    return (
        f"📁 **Send files to generate link**\n\n"
        f"Files received: **{count}**\n\n"
        f"Send any files (video, photo, audio, document etc.)\n"
        f"When done, send /m_link to generate the link."
    )

def _ask_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
    ])


# ── /m_link command ───────────────────────────────────────────────────────────

@Client.on_message(filters.command("m_link") & filters.private)
async def cmd_m_link(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    sess = LINK_SESSIONS.get(uid)
    if not sess or sess["state"] != "collecting":
        await message.reply_text("⚠️ Start with /l first and send some files.")
        return
    if not sess["files"]:
        await message.reply_text("⚠️ No files collected yet. Send at least one file.")
        return

    # Delete the ask message
    try:
        await client.delete_messages(message.chat.id, sess["ask_msg_id"])
    except Exception:
        pass
    await message.delete()

    sess["state"] = "ss_progress"

    # Show progress message
    nav = await client.send_message(
        message.chat.id,
        "⏳ **Generating screenshots…**\n\n`0 / ? done`",
        reply_markup=_progress_markup(),
    )
    sess["nav_msg_id"] = nav.id

    # Start SS generation in background
    task = asyncio.create_task(_generate_ss_bg(client, uid))
    sess["bg_task"] = task


# ── background SS generator ───────────────────────────────────────────────────

async def _generate_ss_bg(client: Client, uid: int):
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    cid = sess["chat_id"]
    nav_id = sess["nav_msg_id"]
    files = sess["files"]
    ss_list: list[str] = []
    shown_times: set[float] = set()   # track used timestamps to avoid repeats

    total = len(files)
    done = 0

    for f in files:
        if sess.get("cancel_flag"):
            return
        if f["media_type"] not in ("video", "animation"):
            # For non-video files, use the file itself as the "screenshot"
            ss_list.append({"file_id": f["file_id"], "media_type": f["media_type"]})
            done += 1
            await _safe_edit(client, cid, nav_id,
                f"⏳ **Generating screenshots…**\n\n`{done} / {total} done`",
                _progress_markup())
            continue

        # For video: grab a frame at a random timestamp
        try:
            ts = _pick_timestamp(shown_times)
            shown_times.add(ts)
            photo_fid = await _grab_frame(client, f["file_id"], ts)
            if photo_fid:
                ss_list.append({"file_id": photo_fid, "media_type": "photo"})
        except Exception as e:
            print(f"[link_gen] SS error: {e}")
        done += 1
        await _safe_edit(client, cid, nav_id,
            f"⏳ **Generating screenshots…**\n\n`{done} / {total} done`",
            _progress_markup())
        await asyncio.sleep(0.1)

    if sess.get("cancel_flag"):
        return

    sess["ss_list"] = ss_list
    sess["ss_index"] = 0
    sess["state"] = "ss_done"

    if not ss_list:
        await _safe_edit(client, cid, nav_id,
            "⚠️ No screenshots could be generated.",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")]]))
        return

    await _show_ss(client, uid, cid, nav_id)


def _pick_timestamp(used: set) -> float:
    for _ in range(20):
        ts = round(random.uniform(1.0, 60.0), 1)
        if ts not in used:
            return ts
    return round(random.uniform(1.0, 60.0), 1)


async def _grab_frame(client: Client, video_file_id: str, ts: float) -> Optional[str]:
    """
    Send the video to the bot itself (or use thumb) to extract a frame.
    Since Telegram doesn't expose a frame-extraction API, we use the video
    thumbnail if available, or we send the video as a photo (will fail for
    real videos), or we return None and the file itself is used.
    """
    # Attempt: forward the video to saved messages and get its thumb
    try:
        # We can't truly extract a frame server-side, so we use the video
        # file_id as the screenshot preview image (thumbnail approach)
        # This sends the first-frame thumb Telegram already has
        sent = await client.send_video(
            "me",
            video_file_id,
            caption=f"ss_ts_{ts}",
        )
        thumb_fid = None
        if sent.video and sent.video.thumbs:
            thumb_fid = sent.video.thumbs[0].file_id
        elif sent.video:
            # Use video itself — caller will show as video preview
            thumb_fid = sent.video.file_id
        try:
            await sent.delete()
        except Exception:
            pass
        return thumb_fid
    except Exception:
        return None


# ── SS navigator ──────────────────────────────────────────────────────────────

async def _show_ss(client: Client, uid: int, cid: int, nav_id: int):
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return
    ss_list = sess["ss_list"]
    idx = sess["ss_index"]
    total = len(ss_list)
    if total == 0:
        return
    item = ss_list[idx]
    caption = f"🖼 Screenshot `{idx + 1} / {total}`"
    markup = _nav_markup(idx, total)

    try:
        if item["media_type"] == "photo":
            await client.edit_message_media(
                cid, nav_id,
                InputMediaPhoto(media=item["file_id"], caption=caption),
                reply_markup=markup,
            )
        else:
            await client.edit_message_media(
                cid, nav_id,
                InputMediaVideo(media=item["file_id"], caption=caption),
                reply_markup=markup,
            )
    except MessageNotModified:
        pass
    except Exception:
        # Fallback: delete & resend
        try:
            await client.delete_messages(cid, nav_id)
        except Exception:
            pass
        if item["media_type"] == "photo":
            new = await client.send_photo(cid, item["file_id"], caption=caption, reply_markup=markup)
        else:
            new = await client.send_video(cid, item["file_id"], caption=caption, reply_markup=markup)
        sess["nav_msg_id"] = new.id


def _nav_markup(idx: int, total: int) -> InlineKeyboardMarkup:
    row1 = []
    if idx > 0:
        row1.append(InlineKeyboardButton("⬅️", callback_data="lg_ss_prev"))
    row1.append(InlineKeyboardButton(f"{idx + 1}/{total}", callback_data="lg_noop"))
    if idx < total - 1:
        row1.append(InlineKeyboardButton("➡️", callback_data="lg_ss_next"))
    return InlineKeyboardMarkup([
        row1,
        [InlineKeyboardButton("🖼 Custom", callback_data="lg_custom"),
         InlineKeyboardButton("♻️ More", callback_data="lg_more_ss")],
        [InlineKeyboardButton("📤 Post", callback_data="lg_post"),
         InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
    ])


def _progress_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Custom", callback_data="lg_custom"),
         InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
    ])


# ── "generate more" SS ────────────────────────────────────────────────────────

async def _generate_more_ss(client: Client, uid: int):
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return
    cid = sess["chat_id"]
    nav_id = sess["nav_msg_id"]
    files = [f for f in sess["files"] if f["media_type"] in ("video", "animation")]
    if not files:
        return

    used_count = len(sess["ss_list"])
    shown_times: set[float] = set()
    new_ss = []

    for f in files:
        if sess.get("cancel_flag"):
            return
        try:
            # Use a timestamp range offset by used_count to avoid repeats
            ts = round(random.uniform(60.0 + used_count * 5, 120.0 + used_count * 10), 1)
            while ts in shown_times:
                ts += 0.5
            shown_times.add(ts)
            photo_fid = await _grab_frame(client, f["file_id"], ts)
            if photo_fid:
                new_ss.append({"file_id": photo_fid, "media_type": "photo"})
        except Exception:
            pass
        await asyncio.sleep(0.1)

    if new_ss:
        sess["ss_list"].extend(new_ss)
        sess["ss_index"] = len(sess["ss_list"]) - len(new_ss)
    sess["state"] = "ss_done"
    await _show_ss(client, uid, cid, nav_id)


# ── post to channel ───────────────────────────────────────────────────────────

async def _post_to_channel(client: Client, uid: int, custom_file: dict = None):
    """Save files to DB, generate link, post to channel."""
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return
    cid = sess["chat_id"]
    nav_id = sess["nav_msg_id"]
    files = sess["files"]

    # Generate post_id / link_id
    if len(files) == 1:
        post_id = files[0]["file_id"][:20]
        link_id = _rand_id(10)
        caption_id = f"`{post_id}`"
    else:
        post_id = _rand_id(12)
        link_id = _rand_id(10)
        caption_id = f"`{post_id}`"

    # Save to DB
    await mdb.async_db["file_links"].insert_one({
        "link_id": link_id,
        "post_id": post_id,
        "files": files,
        "created_at": datetime.now(),
        "created_by": uid,
    })

    bot_me = await client.get_me()
    tg_link = f"https://t.me/{bot_me.username}?start=link_{link_id}"
    caption = (
        f"🔗 **Post Link:** `{tg_link}`\n\n"
        f"🆔 **Post ID:** {caption_id}\n\n"
        f"_(Use /delete {post_id} to remove from DB)_"
    )

    # Determine what to post
    if custom_file:
        preview_fid = custom_file["file_id"]
        preview_type = custom_file["media_type"]
    elif sess["ss_list"]:
        item = sess["ss_list"][sess["ss_index"]]
        preview_fid = item["file_id"]
        preview_type = item["media_type"]
    else:
        preview_fid = files[0]["file_id"]
        preview_type = files[0]["media_type"]

    try:
        if preview_type == "photo":
            await client.send_photo(POST_CHANNEL, preview_fid, caption=caption)
        elif preview_type == "video":
            await client.send_video(POST_CHANNEL, preview_fid, caption=caption)
        else:
            await client.send_document(POST_CHANNEL, preview_fid, caption=caption)
    except Exception as e:
        await client.send_message(cid, f"⚠️ Failed to post: {e}")
        return

    # Show success
    try:
        await client.delete_messages(cid, nav_id)
    except Exception:
        pass
    await client.send_message(
        cid,
        f"✅ **Post sent to channel!**\n\n🔗 Link: `{tg_link}`\n🆔 ID: `{post_id}`",
    )
    _cancel_session(uid)


# ── handle_link_access (called from cmds.py start handler) ───────────────────

async def handle_link_access(client: Client, message: Message, link_id: str):
    from vars import IS_FSUB, PROTECT_CONTENT
    from .fsub import get_fsub
    if IS_FSUB and not await get_fsub(client, message):
        return

    doc = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not doc:
        await message.reply_text("❌ Invalid or expired link.")
        return

    files = doc.get("files", [])
    if not files:
        await message.reply_text("❌ No files found for this link.")
        return

    for f in files:
        try:
            fid = f["file_id"]
            mtype = f["media_type"]
            if mtype == "video":
                await client.send_video(message.chat.id, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "photo":
                await client.send_photo(message.chat.id, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "document":
                await client.send_document(message.chat.id, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "audio":
                await client.send_audio(message.chat.id, fid, protect_content=PROTECT_CONTENT)
            else:
                await client.send_document(message.chat.id, fid, protect_content=PROTECT_CONTENT)
        except Exception as e:
            print(f"[handle_link_access] send error: {e}")
        await asyncio.sleep(0.3)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _safe_edit(client, cid, mid, text, markup):
    try:
        await client.edit_message_text(cid, mid, text, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception:
        pass


def _cancel_session(uid: int):
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return
    sess["cancel_flag"] = True
    task = sess.get("bg_task")
    if task and not task.done():
        task.cancel()
    LINK_SESSIONS.pop(uid, None)


# ── callback dispatcher ───────────────────────────────────────────────────────
# (registered in callback.py via the main callback_query_handler)

async def handle_lg_callback(client: Client, query, data: str):
    uid = query.from_user.id
    if not is_admin(uid):
        await query.answer("Not authorized.", show_alert=True)
        return

    sess = LINK_SESSIONS.get(uid)

    # ── noop ──
    if data == "lg_noop":
        await query.answer()
        return

    # ── cancel ──
    if data == "lg_cancel":
        await query.answer("Cancelled.")
        cid = query.message.chat.id
        nav_id = query.message.id
        _cancel_session(uid)
        try:
            await client.delete_messages(cid, nav_id)
        except Exception:
            pass
        await client.send_message(cid, "❌ Operation cancelled.")
        return

    if not sess:
        await query.answer("No active session. Use /l to start.", show_alert=True)
        return

    cid = sess["chat_id"]
    nav_id = sess["nav_msg_id"]

    # ── SS prev / next ──
    if data == "lg_ss_prev":
        await query.answer()
        sess["ss_index"] = max(0, sess["ss_index"] - 1)
        await _show_ss(client, uid, cid, nav_id)
        return

    if data == "lg_ss_next":
        await query.answer()
        sess["ss_index"] = min(len(sess["ss_list"]) - 1, sess["ss_index"] + 1)
        await _show_ss(client, uid, cid, nav_id)
        return

    # ── generate more ──
    if data == "lg_more_ss":
        await query.answer("Generating more…")
        sess["state"] = "ss_progress"
        # If bg task is done, start a new one for "more"
        task = asyncio.create_task(_generate_more_ss(client, uid))
        sess["bg_task"] = task
        return

    # ── custom thumbnail / file ──
    if data == "lg_custom":
        await query.answer()
        prev_state = sess.get("state", "ss_progress")
        sess["pre_custom_state"] = prev_state
        sess["state"] = "custom_wait"
        try:
            await client.edit_message_caption(
                cid, nav_id,
                caption="📎 **Send a file (photo/video) to use as post preview.**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ Back", callback_data="lg_custom_back")]
                ]),
            )
        except Exception:
            pass
        return

    # ── back from custom ──
    if data == "lg_custom_back":
        await query.answer()
        sess["state"] = sess.get("pre_custom_state", "ss_done")
        if sess["state"] == "ss_done" and sess["ss_list"]:
            await _show_ss(client, uid, cid, nav_id)
        else:
            await _safe_edit(client, cid, nav_id,
                "⏳ **Screenshots still generating…**\n\nPlease wait.",
                _progress_markup())
        return

    # ── post ──
    if data == "lg_post":
        await query.answer("Posting…")
        await _post_to_channel(client, uid)
        return


# ── custom file receiver ──────────────────────────────────────────────────────

def _is_custom_wait(_, __, msg: Message) -> bool:
    if not msg.from_user:
        return False
    uid = msg.from_user.id
    sess = LINK_SESSIONS.get(uid)
    return bool(
        is_admin(uid)
        and sess
        and sess["state"] == "custom_wait"
        and (msg.video or msg.photo or msg.document or msg.animation)
    )

custom_wait_filter = filters.create(_is_custom_wait)

@Client.on_message(custom_wait_filter & filters.private)
async def receive_custom_file(client: Client, message: Message):
    uid = message.from_user.id
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    media, mtype = None, None
    if message.video:       media, mtype = message.video,     "video"
    elif message.photo:     media, mtype = message.photo,     "photo"
    elif message.document:  media, mtype = message.document,  "document"
    elif message.animation: media, mtype = message.animation, "animation"
    if not media:
        return

    custom_file = {"file_id": media.file_id, "media_type": mtype}
    # Cancel any running SS task (custom overrides)
    if sess.get("bg_task") and not sess["bg_task"].done():
        sess["cancel_flag"] = True
        sess["bg_task"].cancel()
        sess["cancel_flag"] = False

    # Post immediately
    await query_answer_safe(client, message.chat.id)
    await _post_to_channel(client, uid, custom_file=custom_file)


async def query_answer_safe(client, chat_id):
    pass  # placeholder — posting is fire-and-forget


# ── stubs kept for callback.py import compatibility ───────────────────────────

async def show_screenshot(*a, **kw): pass
async def generate_screenshots(*a, **kw): pass
async def post_screenshot_to_channel(*a, **kw): pass
async def _cleanup_ss_files(*a, **kw): pass
async def _finish_and_show_navigator(*a, **kw): pass
