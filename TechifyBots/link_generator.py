"""
link_generator.py  — /l  and  /m_link  admin flow
===================================================

FLOW
────
/l          → clears any old session, sends "send files" prompt.
              Every time admin sends a file the prompt is deleted and
              a fresh one with the correct count is sent below the files.

/m_link     → closes the collection phase, saves nothing yet,
              kicks off thumbnail/preview extraction in the background,
              and shows a live progress message.

              Progress message has two buttons:
                [🖼 Custom]   [❌ Cancel]

Cancel      → kills background task, deletes the message, done.

Custom      → edits the message to "Send a file to use as thumbnail"
              with a [↩️ Back] button.
              • Back  → if SS generation still running, show progress msg.
                        if SS done, jump straight to SS navigator.
              • File  → post immediately with that file as preview,
                        then cancel SS task if still running.

SS navigator (after generation finishes):
              ╔══════════════════════════╗
              ║  🖼  Screenshot 2 / 5    ║
              ╚══════════════════════════╝
                [⬅️]  [2/5]  [➡️]
              [🖼 Custom]  [♻️ More SS]
              [📤 Post]    [❌ Cancel]

♻️ More SS  → generate another batch and append; jump to first new one.
📤 Post     → post currently visible SS to POST_CHANNEL with link caption.
❌ Cancel   → clean up and quit.

After posting, bot confirms with the link and post-ID so admin can
later delete with  /delete <post_id>.

DB schema (file_links collection)
──────────────────────────────────
{
  "link_id":    str,          # used in deep-link: ?start=link_<link_id>
  "post_id":    str,          # shown in channel caption for /delete
  "files": [                  # list of files collected via /l
    { "file_id": str, "media_type": str },   ← always both keys
    …
  ],
  "created_at": datetime,
  "created_by": int,
}
"""

from __future__ import annotations

import asyncio
import random
import string
from datetime import datetime
from typing import Optional

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from vars import ADMIN_ID, POST_CHANNEL
from Database.maindb import mdb

# ─── admin check ─────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    """Works whether ADMIN_ID is an int or a list."""
    if isinstance(ADMIN_ID, (list, tuple)):
        return uid in ADMIN_ID
    return uid == ADMIN_ID


# ─── tiny helpers ─────────────────────────────────────────────────────────────

def _rand_id(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


async def _safe_edit_text(client: Client, chat_id: int, msg_id: int,
                          text: str, markup: InlineKeyboardMarkup | None = None):
    try:
        await client.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception:
        pass


async def _safe_delete(client: Client, chat_id: int, msg_id: int):
    try:
        await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


# ─── session store ────────────────────────────────────────────────────────────
#
#  LINK_SESSIONS[admin_id] = {
#    "chat_id":          int,
#    "state":            "collecting" | "ss_progress" | "ss_done" | "custom_wait",
#    "files":            [ {"file_id": str, "media_type": str}, … ],
#    "ask_msg_id":       int | None,   # the "send files" prompt
#    "nav_msg_id":       int | None,   # progress / SS navigator message
#    "ss_list":          [ {"file_id": str, "media_type": str}, … ],
#    "ss_index":         int,
#    "cancel_flag":      bool,
#    "bg_task":          asyncio.Task | None,
#    "pre_custom_state": str,          # state before entering custom_wait
#  }

LINK_SESSIONS: dict[int, dict] = {}

# kept so callback.py import doesn't break
SCREENSHOT_SESSIONS = LINK_SESSIONS
SS_CANCEL_FLAGS: dict = {}
SS_BG_TASKS: dict = {}
SS_DL_CUSTOM_ACTIVE: dict = {}


# ─── session lifecycle ────────────────────────────────────────────────────────

def _new_session(uid: int, chat_id: int) -> dict:
    return {
        "chat_id":          chat_id,
        "state":            "collecting",
        "files":            [],
        "ask_msg_id":       None,
        "nav_msg_id":       None,
        "ss_list":          [],
        "ss_index":         0,
        "cancel_flag":      False,
        "bg_task":          None,
        "pre_custom_state": "ss_progress",
    }


def _kill_session(uid: int):
    sess = LINK_SESSIONS.pop(uid, None)
    if not sess:
        return
    sess["cancel_flag"] = True
    task = sess.get("bg_task")
    if task and not task.done():
        task.cancel()


# ─── markups ──────────────────────────────────────────────────────────────────

def _ask_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
    ])


def _progress_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼 Custom", callback_data="lg_custom"),
            InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel"),
        ],
    ])


def _nav_markup(idx: int, total: int) -> InlineKeyboardMarkup:
    # Row 1: navigation  ← n/total →
    nav_row: list[InlineKeyboardButton] = []
    if idx > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data="lg_ss_prev"))
    nav_row.append(InlineKeyboardButton(f"{idx + 1}/{total}", callback_data="lg_noop"))
    if idx < total - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data="lg_ss_next"))

    return InlineKeyboardMarkup([
        nav_row,
        [
            InlineKeyboardButton("🖼 Custom",  callback_data="lg_custom"),
            InlineKeyboardButton("♻️ More SS", callback_data="lg_more_ss"),
        ],
        [
            InlineKeyboardButton("📤 Post",    callback_data="lg_post"),
            InlineKeyboardButton("❌ Cancel",  callback_data="lg_cancel"),
        ],
    ])


def _custom_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Back", callback_data="lg_custom_back")],
    ])


# ─── ask-text helper ──────────────────────────────────────────────────────────

def _ask_text(count: int) -> str:
    plural = "file" if count == 1 else "files"
    body = (
        f"📁 **Send files to generate a link**\n\n"
        f"✅ Received: **{count} {plural}**\n\n"
        f"Send any file (video · photo · audio · document …)\n"
        f"When done, send /m\\_link to generate the link."
    )
    return body


# ─── /l  command ──────────────────────────────────────────────────────────────

@Client.on_message(filters.command("l") & filters.private)
async def cmd_l(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return
    uid = message.from_user.id

    # Tear down any previous session cleanly
    old = LINK_SESSIONS.get(uid)
    if old and old.get("nav_msg_id"):
        await _safe_delete(client, old["chat_id"], old["nav_msg_id"])
    if old and old.get("ask_msg_id"):
        await _safe_delete(client, old["chat_id"], old["ask_msg_id"])
    _kill_session(uid)

    sess = _new_session(uid, message.chat.id)
    LINK_SESSIONS[uid] = sess

    ask = await message.reply_text(_ask_text(0), reply_markup=_ask_markup())
    sess["ask_msg_id"] = ask.id


# ─── file collector ───────────────────────────────────────────────────────────

def _collecting_filter(_, __, msg: Message) -> bool:
    if not msg.from_user:
        return False
    uid = msg.from_user.id
    sess = LINK_SESSIONS.get(uid)
    return bool(
        sess
        and _is_admin(uid)
        and sess["state"] == "collecting"
        and (
            msg.video or msg.photo or msg.document
            or msg.audio or msg.voice or msg.animation
        )
    )


collecting_filter = filters.create(_collecting_filter)


@Client.on_message(collecting_filter & filters.private)
async def collect_file(client: Client, message: Message):
    uid = message.from_user.id
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    # Extract the media info — always store as clean dict
    file_id: str | None = None
    media_type: str | None = None

    if message.video:
        file_id = message.video.file_id
        media_type = "video"
    elif message.photo:
        file_id = message.photo.file_id
        media_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        media_type = "document"
    elif message.audio:
        file_id = message.audio.file_id
        media_type = "audio"
    elif message.voice:
        file_id = message.voice.file_id
        media_type = "voice"
    elif message.animation:
        file_id = message.animation.file_id
        media_type = "animation"

    if not file_id:
        return

    sess["files"].append({"file_id": file_id, "media_type": media_type})
    count = len(sess["files"])

    # Delete old prompt → send fresh one with updated count
    if sess["ask_msg_id"]:
        await _safe_delete(client, message.chat.id, sess["ask_msg_id"])

    ask = await message.reply_text(_ask_text(count), reply_markup=_ask_markup())
    sess["ask_msg_id"] = ask.id


# ─── /m_link  command ─────────────────────────────────────────────────────────

@Client.on_message(filters.command("m_link") & filters.private)
async def cmd_m_link(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    sess = LINK_SESSIONS.get(uid)

    if not sess or sess["state"] != "collecting":
        await message.reply_text(
            "⚠️ No active collection. Use /l first, then send your files."
        )
        return

    if not sess["files"]:
        await message.reply_text(
            "⚠️ You haven't sent any files yet. Send at least one file."
        )
        return

    # Remove the /m_link command message and the ask prompt
    await _safe_delete(client, message.chat.id, message.id)
    if sess["ask_msg_id"]:
        await _safe_delete(client, message.chat.id, sess["ask_msg_id"])
        sess["ask_msg_id"] = None

    sess["state"] = "ss_progress"
    total = len(sess["files"])

    nav = await client.send_message(
        message.chat.id,
        f"⏳ **Generating previews…**\n\n`0 / {total}` done",
        reply_markup=_progress_markup(),
    )
    sess["nav_msg_id"] = nav.id

    task = asyncio.create_task(_generate_ss_bg(client, uid))
    sess["bg_task"] = task


# ─── background SS generator ──────────────────────────────────────────────────

async def _generate_ss_bg(client: Client, uid: int):
    """
    For each file:
      • video / animation → try to grab a thumbnail from Telegram
      • anything else     → use the file itself as the preview item
    Results are appended to sess["ss_list"] as clean dicts.
    """
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    chat_id   = sess["chat_id"]
    nav_id    = sess["nav_msg_id"]
    files     = sess["files"]
    total     = len(files)
    ss_list   = sess["ss_list"]

    for i, f in enumerate(files):
        if sess.get("cancel_flag"):
            return

        fid   = f["file_id"]
        mtype = f["media_type"]

        if mtype in ("video", "animation"):
            thumb_fid = await _extract_thumb(client, fid)
            if thumb_fid:
                ss_list.append({"file_id": thumb_fid, "media_type": "photo"})
            else:
                # Fall back: use the video itself as preview
                ss_list.append({"file_id": fid, "media_type": mtype})
        else:
            ss_list.append({"file_id": fid, "media_type": mtype})

        done = i + 1
        await _safe_edit_text(
            client, chat_id, nav_id,
            f"⏳ **Generating previews…**\n\n`{done} / {total}` done",
            _progress_markup(),
        )
        await asyncio.sleep(0.05)

    if sess.get("cancel_flag"):
        return

    sess["state"]    = "ss_done"
    sess["ss_index"] = 0

    if not ss_list:
        await _safe_edit_text(
            client, chat_id, nav_id,
            "⚠️ Could not generate any previews.\n\nUse 🖼 Custom to pick one manually.",
            _progress_markup(),
        )
        return

    await _render_ss(client, uid)


async def _extract_thumb(client: Client, video_fid: str) -> Optional[str]:
    """
    Send the video to the bot's Saved Messages, read its thumbnail file_id,
    delete the forwarded message, return the thumb file_id (or None).
    """
    try:
        sent = await client.send_video("me", video_fid)
        thumb_fid: str | None = None
        if sent.video and sent.video.thumbs:
            thumb_fid = sent.video.thumbs[0].file_id
        await _safe_delete(client, sent.chat.id, sent.id)
        return thumb_fid
    except Exception:
        return None


# ─── SS navigator rendering ───────────────────────────────────────────────────

async def _render_ss(client: Client, uid: int):
    """Edit the nav message to show the current screenshot."""
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    ss_list = sess["ss_list"]
    idx     = sess["ss_index"]
    total   = len(ss_list)
    chat_id = sess["chat_id"]
    nav_id  = sess["nav_msg_id"]

    if total == 0 or idx >= total:
        return

    item    = ss_list[idx]
    fid     = item["file_id"]
    mtype   = item["media_type"]
    caption = f"🖼 **Preview  {idx + 1} / {total}**"
    markup  = _nav_markup(idx, total)

    try:
        if mtype == "photo":
            await client.edit_message_media(
                chat_id, nav_id,
                InputMediaPhoto(media=fid, caption=caption),
                reply_markup=markup,
            )
        elif mtype in ("video", "animation"):
            await client.edit_message_media(
                chat_id, nav_id,
                InputMediaVideo(media=fid, caption=caption),
                reply_markup=markup,
            )
        else:
            # document / audio / voice — can't embed as media; show text nav
            await _safe_edit_text(
                client, chat_id, nav_id,
                f"📄 **Preview  {idx + 1} / {total}**\n\n"
                f"File type: `{mtype}` — thumbnail not available.",
                markup,
            )
    except MessageNotModified:
        pass
    except Exception:
        # Fallback: delete old message, send new one
        await _safe_delete(client, chat_id, nav_id)
        if mtype == "photo":
            new = await client.send_photo(chat_id, fid, caption=caption, reply_markup=markup)
        elif mtype in ("video", "animation"):
            new = await client.send_video(chat_id, fid, caption=caption, reply_markup=markup)
        else:
            new = await client.send_message(
                chat_id,
                f"📄 **Preview  {idx + 1} / {total}**\n\nFile type: `{mtype}`",
                reply_markup=markup,
            )
        sess["nav_msg_id"] = new.id


# ─── "more SS" generator ──────────────────────────────────────────────────────

async def _generate_more_ss(client: Client, uid: int):
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    chat_id   = sess["chat_id"]
    nav_id    = sess["nav_msg_id"]
    files     = [f for f in sess["files"] if f["media_type"] in ("video", "animation")]
    old_count = len(sess["ss_list"])

    if not files:
        await _safe_edit_text(
            client, chat_id, nav_id,
            "⚠️ No video files to generate more previews from.",
            _nav_markup(sess["ss_index"], len(sess["ss_list"])),
        )
        return

    await _safe_edit_text(
        client, chat_id, nav_id,
        "⏳ **Generating more previews…**",
        _progress_markup(),
    )

    for f in files:
        if sess.get("cancel_flag"):
            return
        # Re-extract; Telegram may return a different thumb each time
        thumb_fid = await _extract_thumb(client, f["file_id"])
        if thumb_fid:
            sess["ss_list"].append({"file_id": thumb_fid, "media_type": "photo"})
        await asyncio.sleep(0.1)

    new_count = len(sess["ss_list"])
    if new_count == old_count:
        # Nothing new — just go back to navigator
        pass
    else:
        sess["ss_index"] = old_count   # jump to first new SS

    sess["state"] = "ss_done"
    await _render_ss(client, uid)


# ─── post to channel ──────────────────────────────────────────────────────────

async def _do_post(client: Client, uid: int, custom_file: dict | None = None):
    """Save to DB → post to POST_CHANNEL → confirm to admin."""
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    chat_id = sess["chat_id"]
    nav_id  = sess["nav_msg_id"]
    files   = sess["files"]

    if not files:
        await _safe_edit_text(client, chat_id, nav_id,
                              "⚠️ No files to post.", None)
        return

    # Build unique IDs
    link_id = _rand_id(10)
    post_id = _rand_id(12)

    # Persist to DB — files already stored as clean dicts
    await mdb.async_db["file_links"].insert_one({
        "link_id":    link_id,
        "post_id":    post_id,
        "files":      files,           # [{"file_id":…,"media_type":…}, …]
        "created_at": datetime.now(),
        "created_by": uid,
    })

    bot_me   = await client.get_me()
    tg_link  = f"https://t.me/{bot_me.username}?start=link_{link_id}"
    caption  = (
        f"🔗 **Link:** `{tg_link}`\n\n"
        f"🆔 **Post ID:** `{post_id}`\n\n"
        f"_Use /delete {post_id} to remove from DB_"
    )

    # Choose preview media
    if custom_file:
        preview = custom_file
    elif sess["ss_list"]:
        preview = sess["ss_list"][sess["ss_index"]]
    else:
        preview = files[0]

    p_fid   = preview["file_id"]
    p_mtype = preview["media_type"]

    try:
        if p_mtype == "photo":
            await client.send_photo(POST_CHANNEL, p_fid, caption=caption)
        elif p_mtype in ("video", "animation"):
            await client.send_video(POST_CHANNEL, p_fid, caption=caption)
        elif p_mtype == "document":
            await client.send_document(POST_CHANNEL, p_fid, caption=caption)
        elif p_mtype == "audio":
            await client.send_audio(POST_CHANNEL, p_fid, caption=caption)
        else:
            await client.send_document(POST_CHANNEL, p_fid, caption=caption)
    except Exception as err:
        await client.send_message(chat_id,
                                  f"⚠️ Failed to post to channel.\n\nError: `{err}`")
        return

    # Success — delete nav message, send confirmation
    await _safe_delete(client, chat_id, nav_id)
    await client.send_message(
        chat_id,
        f"✅ **Posted to channel!**\n\n"
        f"🔗 Link: `{tg_link}`\n"
        f"🆔 Post ID: `{post_id}`\n\n"
        f"Use /delete `{post_id}` to remove from DB.",
    )

    # Tear down session
    _kill_session(uid)


# ─── callback dispatcher (called from callback.py) ────────────────────────────

async def handle_lg_callback(client: Client, query, data: str):
    uid = query.from_user.id
    if not _is_admin(uid):
        await query.answer("❌ Not authorised.", show_alert=True)
        return

    # ── noop (counter button) ──────────────────────────────────────────────
    if data == "lg_noop":
        await query.answer()
        return

    # ── cancel ────────────────────────────────────────────────────────────
    if data == "lg_cancel":
        await query.answer("Cancelled ✅")
        cid    = query.message.chat.id
        nav_id = query.message.id
        _kill_session(uid)
        await _safe_delete(client, cid, nav_id)
        await client.send_message(cid, "❌ Operation cancelled.")
        return

    sess = LINK_SESSIONS.get(uid)
    if not sess:
        await query.answer("No active session. Use /l to start.", show_alert=True)
        return

    chat_id = sess["chat_id"]
    nav_id  = sess["nav_msg_id"]

    # ── SS prev ───────────────────────────────────────────────────────────
    if data == "lg_ss_prev":
        await query.answer()
        if sess["ss_index"] > 0:
            sess["ss_index"] -= 1
            await _render_ss(client, uid)
        return

    # ── SS next ───────────────────────────────────────────────────────────
    if data == "lg_ss_next":
        await query.answer()
        if sess["ss_index"] < len(sess["ss_list"]) - 1:
            sess["ss_index"] += 1
            await _render_ss(client, uid)
        return

    # ── more SS ───────────────────────────────────────────────────────────
    if data == "lg_more_ss":
        await query.answer("Generating more…")
        task = asyncio.create_task(_generate_more_ss(client, uid))
        sess["bg_task"] = task
        return

    # ── custom thumbnail ──────────────────────────────────────────────────
    if data == "lg_custom":
        await query.answer()
        sess["pre_custom_state"] = sess["state"]
        sess["state"] = "custom_wait"
        try:
            await client.edit_message_caption(
                chat_id, nav_id,
                caption=(
                    "📎 **Send a file to use as the post preview.**\n\n"
                    "Supported: photo · video · document"
                ),
                reply_markup=_custom_markup(),
            )
        except Exception:
            # If the current message has no caption (text-only), edit text instead
            try:
                await client.edit_message_text(
                    chat_id, nav_id,
                    "📎 **Send a file to use as the post preview.**\n\n"
                    "Supported: photo · video · document",
                    reply_markup=_custom_markup(),
                )
            except Exception:
                pass
        return

    # ── back from custom ──────────────────────────────────────────────────
    if data == "lg_custom_back":
        await query.answer()
        prev = sess.get("pre_custom_state", "ss_progress")
        sess["state"] = prev

        if prev == "ss_done" and sess["ss_list"]:
            await _render_ss(client, uid)
        else:
            await _safe_edit_text(
                client, chat_id, nav_id,
                "⏳ **Still generating previews… please wait.**",
                _progress_markup(),
            )
        return

    # ── post ──────────────────────────────────────────────────────────────
    if data == "lg_post":
        await query.answer("Posting…")
        await _do_post(client, uid)
        return


# ─── custom-file receiver ─────────────────────────────────────────────────────

def _custom_wait_filter(_, __, msg: Message) -> bool:
    if not msg.from_user:
        return False
    uid  = msg.from_user.id
    sess = LINK_SESSIONS.get(uid)
    return bool(
        sess
        and _is_admin(uid)
        and sess["state"] == "custom_wait"
        and (msg.video or msg.photo or msg.document or msg.animation)
    )


custom_wait_filter = filters.create(_custom_wait_filter)


@Client.on_message(custom_wait_filter & filters.private)
async def receive_custom_file(client: Client, message: Message):
    uid  = message.from_user.id
    sess = LINK_SESSIONS.get(uid)
    if not sess:
        return

    file_id:    str | None = None
    media_type: str | None = None

    if message.video:
        file_id, media_type = message.video.file_id,     "video"
    elif message.photo:
        file_id, media_type = message.photo.file_id,     "photo"
    elif message.document:
        file_id, media_type = message.document.file_id,  "document"
    elif message.animation:
        file_id, media_type = message.animation.file_id, "animation"

    if not file_id:
        return

    custom_file = {"file_id": file_id, "media_type": media_type}

    # Cancel any running SS task — custom overrides it
    sess["cancel_flag"] = True
    if sess.get("bg_task") and not sess["bg_task"].done():
        sess["bg_task"].cancel()
    sess["cancel_flag"] = False      # reset so _do_post works normally

    await _do_post(client, uid, custom_file=custom_file)


# ─── handle_link_access  (called from cmds.py  /start  handler) ──────────────

async def handle_link_access(client: Client, message: Message, link_id: str):
    """Send all files stored under link_id to the user."""
    from vars import IS_FSUB, PROTECT_CONTENT
    from .fsub import get_fsub

    if IS_FSUB and not await get_fsub(client, message):
        return

    doc = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not doc:
        await message.reply_text("❌ This link is invalid or has expired.")
        return

    raw_files = doc.get("files", [])
    if not raw_files:
        await message.reply_text("❌ No files found for this link.")
        return

    # ── normalise every entry ─────────────────────────────────────────────
    # Old sessions may have stored raw Pyrogram objects serialised by Motor,
    # or dicts missing "media_type".  We handle all cases gracefully.
    files: list[dict] = []
    for f in raw_files:
        if isinstance(f, dict):
            fid   = f.get("file_id") or f.get("file_id_str") or ""
            mtype = f.get("media_type") or f.get("type") or "document"
        else:
            # Unexpected type — skip
            continue
        if fid:
            files.append({"file_id": fid, "media_type": mtype})

    if not files:
        await message.reply_text("❌ No valid files found for this link.")
        return

    cid = message.chat.id

    for f in files:
        fid   = f["file_id"]
        mtype = f["media_type"]
        try:
            if mtype == "video":
                await client.send_video(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "photo":
                await client.send_photo(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "audio":
                await client.send_audio(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "voice":
                await client.send_voice(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "animation":
                await client.send_animation(cid, fid, protect_content=PROTECT_CONTENT)
            else:
                # document / unknown
                await client.send_document(cid, fid, protect_content=PROTECT_CONTENT)
        except Exception as err:
            print(f"[handle_link_access] send error for {mtype}: {err}")
        await asyncio.sleep(0.3)   # small delay between files


# ─── stubs so existing  callback.py  import line doesn't explode ─────────────

async def show_screenshot(*a, **kw):          pass
async def generate_screenshots(*a, **kw):     pass
async def post_screenshot_to_channel(*a, **kw): pass
async def _cleanup_ss_files(*a, **kw):        pass
async def _finish_and_show_navigator(*a, **kw): pass
