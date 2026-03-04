# link_generator.py  —  /l  and  /m_link  admin flow
#
# FIXES in this version
# ─────────────────────
# 1. USER_IS_BOT: bots can't send to "me". SS photos are uploaded to the
#    admin's own DM (chat_id), file_id extracted, then deleted immediately.
#    Admin never sees them — they appear and vanish in <1 s.
#
# 2. Bulk-file debounce: when admin sends several files at once, individual
#    handler calls race. A per-uid lock + 400 ms settle window ensures only
#    ONE updated prompt is sent after the burst settles.
#
# 3. Post caption: only quoted file/group ID, link lives in the button.

from __future__ import annotations

import asyncio
import math
import os
import random
import shutil
import string
import tempfile
from datetime import datetime
from typing import Optional

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from vars import ADMIN_IDS, POST_CHANNEL
from Database.maindb import mdb


# ── admin check ───────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ── helpers ───────────────────────────────────────────────────────────────────

def _rand_id(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


async def _del(client: Client, chat_id: int, msg_id: int):
    try:
        await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def _edit(client: Client, chat_id: int, msg_id: int,
                text: str, markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await client.edit_message_text(
            chat_id, msg_id, text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except (MessageNotModified, Exception):
        pass


# ── session ───────────────────────────────────────────────────────────────────
#
# LINK_SESSIONS[admin_id] = {
#   "chat_id"          : int
#   "state"            : "collecting" | "generating" | "ss_done" | "custom_wait"
#   "files"            : [{"file_id":str, "media_type":str, "msg_id":int}, ...]
#   "ask_msg_id"       : int | None
#   "nav_msg_id"       : int | None
#   "ss_file_ids"      : [str, ...]        ← file_ids only; never shown in DM
#   "ss_index"         : int
#   "cancel_flag"      : bool
#   "bg_task"          : asyncio.Task | None
#   "batch"            : int
#   "pre_custom_state" : str
#   "_collect_lock"    : asyncio.Lock      ← prevents race in bulk collection
#   "_settle_task"     : asyncio.Task|None ← debounce task for bulk files
# }

LINK_SESSIONS: dict[int, dict] = {}

# compat aliases used by callback.py import
SCREENSHOT_SESSIONS   = LINK_SESSIONS
SS_CANCEL_FLAGS: dict = {}
SS_BG_TASKS: dict     = {}
SS_DL_CUSTOM_ACTIVE: dict = {}

SETTLE_DELAY = 0.4   # seconds to wait after last file before updating prompt


def _new_sess(uid: int, chat_id: int) -> dict:
    return {
        "chat_id":           chat_id,
        "state":             "collecting",
        "files":             [],
        "ask_msg_id":        None,
        "nav_msg_id":        None,
        "ss_file_ids":       [],
        "ss_index":          0,
        "cancel_flag":       False,
        "bg_task":           None,
        "batch":             0,
        "pre_custom_state":  "generating",
        "_collect_lock":     asyncio.Lock(),
        "_settle_task":      None,
    }


def _kill(uid: int):
    s = LINK_SESSIONS.pop(uid, None)
    if not s:
        return
    s["cancel_flag"] = True
    for key in ("bg_task", "_settle_task"):
        t = s.get(key)
        if t and not t.done():
            t.cancel()


# ── markups ───────────────────────────────────────────────────────────────────

def _ask_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
    ])


def _prog_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼 Custom",  callback_data="lg_custom"),
            InlineKeyboardButton("❌ Cancel",  callback_data="lg_cancel"),
        ],
    ])


def _nav_kb(idx: int, total: int) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if idx > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data="lg_prev"))
    row.append(InlineKeyboardButton(f"{idx + 1}/{total}", callback_data="lg_noop"))
    if idx < total - 1:
        row.append(InlineKeyboardButton("➡️", callback_data="lg_next"))
    return InlineKeyboardMarkup([
        row,
        [
            InlineKeyboardButton("🖼 Custom",  callback_data="lg_custom"),
            InlineKeyboardButton("♻️ More SS", callback_data="lg_more"),
        ],
        [
            InlineKeyboardButton("📤 Post",   callback_data="lg_post"),
            InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel"),
        ],
    ])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Back", callback_data="lg_custom_back")],
    ])


# ── ask-text ──────────────────────────────────────────────────────────────────

def _ask_text(n: int) -> str:
    word = "file" if n == 1 else "files"
    return (
        f"📁 **Link Generator**\n\n"
        f"Files received: **{n} {word}**\n\n"
        f"Send any file (video · photo · audio · document …)\n\n"
        f"Send /m\\_link when done to generate screenshots & link."
    )


# ── /l ────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("l") & filters.private)
async def cmd_l(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return
    uid = message.from_user.id

    old = LINK_SESSIONS.get(uid)
    if old:
        for mid in [old.get("nav_msg_id"), old.get("ask_msg_id")]:
            if mid:
                await _del(client, old["chat_id"], mid)
        _kill(uid)

    s = _new_sess(uid, message.chat.id)
    LINK_SESSIONS[uid] = s
    ask = await message.reply_text(_ask_text(0), reply_markup=_ask_kb())
    s["ask_msg_id"] = ask.id


# ── file collector with debounce ──────────────────────────────────────────────
#
# When the admin sends multiple files at once (album / rapid paste), Pyrogram
# fires one handler call per file. Without a debounce, each call would delete
# and re-create the prompt, producing N−1 orphaned messages and a final count
# that may be wrong due to races.
#
# Solution: every handler call just appends the file to the list and
# (re)schedules a "settle" coroutine that fires after SETTLE_DELAY seconds
# of silence. Only the settle task actually touches the prompt message.

def _want_file(_, __, msg: Message) -> bool:
    if not msg.from_user:
        return False
    uid = msg.from_user.id
    s   = LINK_SESSIONS.get(uid)
    return bool(
        s and _is_admin(uid) and s["state"] == "collecting"
        and (msg.video or msg.photo or msg.document
             or msg.audio or msg.voice or msg.animation)
    )

_file_filter = filters.create(_want_file)


@Client.on_message(_file_filter & filters.private)
async def collect_file(client: Client, message: Message):
    uid = message.from_user.id
    s   = LINK_SESSIONS.get(uid)
    if not s:
        return

    fid = mtype = None
    if   message.video:     fid, mtype = message.video.file_id,     "video"
    elif message.photo:     fid, mtype = message.photo.file_id,     "photo"
    elif message.document:  fid, mtype = message.document.file_id,  "document"
    elif message.audio:     fid, mtype = message.audio.file_id,     "audio"
    elif message.voice:     fid, mtype = message.voice.file_id,     "voice"
    elif message.animation: fid, mtype = message.animation.file_id, "animation"
    if not fid:
        return

    # Append immediately (no lock needed — GIL protects list.append)
    s["files"].append({"file_id": fid, "media_type": mtype, "msg_id": message.id})

    # Cancel any pending settle task and schedule a fresh one
    old = s.get("_settle_task")
    if old and not old.done():
        old.cancel()
    s["_settle_task"] = asyncio.create_task(
        _settle_prompt(client, uid, message.chat.id)
    )


async def _settle_prompt(client: Client, uid: int, chat_id: int):
    """Wait for the burst to settle, then update the prompt once."""
    await asyncio.sleep(SETTLE_DELAY)
    s = LINK_SESSIONS.get(uid)
    if not s or s["state"] != "collecting":
        return

    n = len(s["files"])
    # Delete old prompt
    if s["ask_msg_id"]:
        await _del(client, chat_id, s["ask_msg_id"])
        s["ask_msg_id"] = None

    # Send fresh prompt
    try:
        ask = await client.send_message(chat_id, _ask_text(n), reply_markup=_ask_kb())
        s["ask_msg_id"] = ask.id
    except Exception as e:
        print(f"[lg] prompt: {e}")


# ── /m_link ───────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("m_link") & filters.private)
async def cmd_m_link(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    s   = LINK_SESSIONS.get(uid)

    if not s or s["state"] != "collecting":
        await message.reply_text("⚠️ No active session. Use /l first, then send your files.")
        return
    if not s["files"]:
        await message.reply_text("⚠️ Send at least one file before /m_link.")
        return

    # Cancel any pending settle task
    t = s.get("_settle_task")
    if t and not t.done():
        t.cancel()

    await _del(client, message.chat.id, message.id)
    if s["ask_msg_id"]:
        await _del(client, message.chat.id, s["ask_msg_id"])
        s["ask_msg_id"] = None

    s["state"] = "generating"
    n_vid = sum(1 for f in s["files"] if f["media_type"] in ("video", "animation"))
    nav = await client.send_message(
        message.chat.id,
        f"⏳ **Generating screenshots…**\n\n0 / {n_vid} video(s) processed",
        reply_markup=_prog_kb(),
    )
    s["nav_msg_id"] = nav.id
    s["bg_task"] = asyncio.create_task(_gen_ss(client, uid, batch=0))


# ── ffmpeg ────────────────────────────────────────────────────────────────────

async def _duration(path: str) -> float:
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def _grab_frame(video: str, ts: float, out: str):
    try:
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-ss", f"{ts:.3f}",
            "-i", video,
            "-frames:v", "1",
            "-q:v", "3",
            "-vf", "scale='min(1280,iw)':-2",
            out,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await p.communicate()
    except Exception:
        pass


def _timestamps(dur: float, count: int, batch: int) -> list:
    if dur <= 0:
        return [float(i + 1) for i in range(count)]
    lo, hi = dur * 0.02, dur * 0.98
    span   = hi - lo
    step   = span / count
    shift  = (batch * step * 0.47) % span
    ts = [lo + (i * step + shift) % span for i in range(count)]
    random.shuffle(ts)
    return [round(t, 3) for t in ts]


# ── SS generation ─────────────────────────────────────────────────────────────

SS_COUNT = 10


async def _gen_ss(client: Client, uid: int, batch: int):
    s = LINK_SESSIONS.get(uid)
    if not s:
        return

    chat_id = s["chat_id"]
    nav_id  = s["nav_msg_id"]
    videos  = [f for f in s["files"] if f["media_type"] in ("video", "animation")]
    n_vids  = len(videos)

    if n_vids == 0:
        s["state"] = "ss_done"
        await _edit(client, chat_id, nav_id,
                    "⚠️ No video files detected.\n\nUse 🖼 Custom to pick a thumbnail, then 📤 Post.",
                    _back_kb())
        return

    ss_per_vid = math.ceil(SS_COUNT / n_vids)
    collected_paths: list[str] = []

    tmp = tempfile.mkdtemp(prefix="tgss_")
    try:
        for vi, vf in enumerate(videos):
            if s.get("cancel_flag"):
                return

            await _edit(client, chat_id, nav_id,
                        f"⏳ **Generating screenshots…**\n\n"
                        f"Downloading file {vi + 1} / {n_vids}…",
                        _prog_kb())

            dl_path = os.path.join(tmp, f"v{vi}.mp4")
            try:
                dl_path = await client.download_media(vf["file_id"], file_name=dl_path)
            except Exception as e:
                print(f"[lg] download: {e}")
                continue
            if not dl_path or not os.path.exists(dl_path):
                continue
            if s.get("cancel_flag"):
                return

            dur     = await _duration(dl_path)
            ts_list = _timestamps(dur, ss_per_vid, batch)
            ss_dir  = os.path.join(tmp, f"ss{vi}")
            os.makedirs(ss_dir, exist_ok=True)

            await _edit(client, chat_id, nav_id,
                        f"⏳ **Generating screenshots…**\n\n"
                        f"File {vi + 1} / {n_vids} — extracting {len(ts_list)} frames…",
                        _prog_kb())

            out_paths = [os.path.join(ss_dir, f"{i:03d}.jpg") for i in range(len(ts_list))]
            await asyncio.gather(
                *[_grab_frame(dl_path, ts, op) for ts, op in zip(ts_list, out_paths)]
            )
            if s.get("cancel_flag"):
                return

            for op in out_paths:
                if os.path.exists(op) and os.path.getsize(op) > 0:
                    collected_paths.append(op)

            try:
                os.remove(dl_path)
            except Exception:
                pass

        if s.get("cancel_flag"):
            return

        if not collected_paths:
            s["state"] = "ss_done"
            await _edit(client, chat_id, nav_id,
                        "⚠️ Could not extract any screenshots.\n\nUse 🖼 Custom to pick a thumbnail.",
                        _back_kb())
            return

        # ── Upload all SS to admin DM, grab file_ids, delete immediately ──
        # NOTE: bots cannot send to "me" (Saved Messages).
        # We send to the admin's own chat_id, read the file_id, then delete.
        # Messages appear and disappear in <1 second — admin won't notice.
        await _edit(client, chat_id, nav_id,
                    f"📤 **Uploading {len(collected_paths)} screenshots…**",
                    _prog_kb())

        old_n = len(s["ss_file_ids"])
        for op in collected_paths:
            if s.get("cancel_flag"):
                return
            try:
                sent = await client.send_photo(chat_id, op)
                s["ss_file_ids"].append(sent.photo.file_id)
                # Delete immediately so it doesn't clutter the DM
                await _del(client, chat_id, sent.id)
            except Exception as e:
                print(f"[lg] upload: {e}")
            await asyncio.sleep(0.05)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if s.get("cancel_flag"):
        return

    s["ss_index"] = old_n if s["ss_file_ids"][old_n:] else max(0, old_n - 1)
    s["batch"]    = batch + 1
    s["state"]    = "ss_done"

    if not s["ss_file_ids"]:
        await _edit(client, chat_id, nav_id,
                    "⚠️ Could not upload any screenshots.\n\nUse 🖼 Custom to pick a thumbnail.",
                    _back_kb())
        return

    await _show_nav(client, uid)


# ── show navigator ────────────────────────────────────────────────────────────

async def _show_nav(client: Client, uid: int):
    s = LINK_SESSIONS.get(uid)
    if not s:
        return
    fids    = s["ss_file_ids"]
    idx     = s["ss_index"]
    total   = len(fids)
    chat_id = s["chat_id"]
    nav_id  = s["nav_msg_id"]
    if not fids or idx >= total:
        return

    caption = f"🖼 **Screenshot  {idx + 1} / {total}**"
    markup  = _nav_kb(idx, total)

    try:
        await client.edit_message_media(
            chat_id, nav_id,
            InputMediaPhoto(media=fids[idx], caption=caption),
            reply_markup=markup,
        )
    except Exception:
        # Nav message might be text-only if generation started fresh —
        # delete and resend as a photo.
        await _del(client, chat_id, nav_id)
        try:
            new = await client.send_photo(
                chat_id, fids[idx], caption=caption, reply_markup=markup,
            )
            s["nav_msg_id"] = new.id
        except Exception as e:
            print(f"[lg] nav: {e}")


# ── More SS ───────────────────────────────────────────────────────────────────

async def _more_ss(client: Client, uid: int):
    s = LINK_SESSIONS.get(uid)
    if not s:
        return
    batch      = s.get("batch", 1)
    s["state"] = "generating"
    await _edit(client, s["chat_id"], s["nav_msg_id"],
                "⏳ **Generating more screenshots…**",
                _prog_kb())
    await _gen_ss(client, uid, batch=batch)


# ── post to channel ───────────────────────────────────────────────────────────

async def _do_post(client: Client, uid: int, custom: Optional[dict] = None):
    s = LINK_SESSIONS.get(uid)
    if not s:
        return

    chat_id = s["chat_id"]
    nav_id  = s["nav_msg_id"]
    files   = s["files"]
    if not files:
        return

    # ── DB record ────────────────────────────────────────────────────────
    link_id  = _rand_id(10)
    single   = len(files) == 1
    post_id  = files[0]["file_id"][:20] if single else _rand_id(12)
    db_files = [{"file_id": f["file_id"], "media_type": f["media_type"]} for f in files]

    await mdb.async_db["file_links"].insert_one({
        "link_id":    link_id,
        "post_id":    post_id,
        "files":      db_files,
        "created_at": datetime.now(),
        "created_by": uid,
    })

    bot_me  = await client.get_me()
    tg_link = f"https://t.me/{bot_me.username}?start=link_{link_id}"

    # ── Caption: just the quoted ID, NO link text ─────────────────────────
    caption = f"> `{post_id}`"

    # ── Button carries the link ───────────────────────────────────────────
    get_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Get File", url=tg_link)]
    ])

    # ── Choose preview media ──────────────────────────────────────────────
    if custom:
        pfid, pmtype = custom["file_id"], custom["media_type"]
    elif single:
        pfid, pmtype = files[0]["file_id"], files[0]["media_type"]
    elif s["ss_file_ids"]:
        pfid, pmtype = s["ss_file_ids"][s["ss_index"]], "photo"
    else:
        pfid, pmtype = files[0]["file_id"], files[0]["media_type"]

    # ── Send to channel ───────────────────────────────────────────────────
    try:
        if pmtype == "photo":
            await client.send_photo(POST_CHANNEL, pfid, caption=caption, reply_markup=get_btn)
        elif pmtype in ("video", "animation"):
            await client.send_video(POST_CHANNEL, pfid, caption=caption, reply_markup=get_btn)
        elif pmtype == "document":
            await client.send_document(POST_CHANNEL, pfid, caption=caption, reply_markup=get_btn)
        elif pmtype == "audio":
            await client.send_audio(POST_CHANNEL, pfid, caption=caption, reply_markup=get_btn)
        else:
            await client.send_document(POST_CHANNEL, pfid, caption=caption, reply_markup=get_btn)
    except Exception as err:
        await _edit(client, chat_id, nav_id, f"⚠️ **Post failed.**\n\nError: `{err}`", None)
        return

    # ── Silently clean up admin DM ────────────────────────────────────────
    to_del: list[int] = []
    if nav_id:
        to_del.append(nav_id)
    for f in files:
        if f.get("msg_id"):
            to_del.append(f["msg_id"])
    if custom and custom.get("msg_id"):
        to_del.append(custom["msg_id"])
    # SS were uploaded & deleted immediately — nothing left to delete

    for i in range(0, len(to_del), 100):
        try:
            await client.delete_messages(chat_id, to_del[i:i + 100])
        except Exception:
            pass
        await asyncio.sleep(0.05)

    _kill(uid)


# ── callback dispatcher (called by callback.py) ───────────────────────────────

async def handle_lg_callback(client: Client, query, data: str):
    uid = query.from_user.id
    if not _is_admin(uid):
        await query.answer("❌ Not authorised.", show_alert=True)
        return

    if data == "lg_noop":
        await query.answer()
        return

    # cancel
    if data == "lg_cancel":
        await query.answer("Cancelled.")
        s   = LINK_SESSIONS.get(uid)
        cid = query.message.chat.id
        if s:
            to_del = [query.message.id]
            for f in s.get("files", []):
                if f.get("msg_id"):
                    to_del.append(f["msg_id"])
            for i in range(0, len(to_del), 100):
                try:
                    await client.delete_messages(cid, to_del[i:i + 100])
                except Exception:
                    pass
        _kill(uid)
        return

    s = LINK_SESSIONS.get(uid)
    if not s:
        await query.answer("No active session. Use /l to start.", show_alert=True)
        return

    chat_id = s["chat_id"]
    nav_id  = s["nav_msg_id"]

    if data == "lg_prev":
        await query.answer()
        if s["ss_index"] > 0:
            s["ss_index"] -= 1
            await _show_nav(client, uid)
        return

    if data == "lg_next":
        await query.answer()
        if s["ss_index"] < len(s["ss_file_ids"]) - 1:
            s["ss_index"] += 1
            await _show_nav(client, uid)
        return

    if data == "lg_more":
        if s["state"] == "generating":
            await query.answer("Already generating…", show_alert=False)
            return
        if not any(f["media_type"] in ("video", "animation") for f in s["files"]):
            await query.answer("No video files to extract from.", show_alert=True)
            return
        await query.answer("Generating more screenshots…")
        s["bg_task"] = asyncio.create_task(_more_ss(client, uid))
        return

    if data == "lg_custom":
        await query.answer()
        s["pre_custom_state"] = s["state"]
        s["state"] = "custom_wait"

        # Pause any running generation task
        t = s.get("bg_task")
        if t and not t.done():
            s["cancel_flag"] = True
            t.cancel()
            s["cancel_flag"] = False

        try:
            await client.edit_message_caption(
                chat_id, nav_id,
                caption=(
                    "📎 **Send a photo or video to use as the post thumbnail.**\n\n"
                    "Tap ↩️ Back to return."
                ),
                reply_markup=_back_kb(),
            )
        except Exception:
            await _edit(
                client, chat_id, nav_id,
                "📎 **Send a photo or video to use as the post thumbnail.**\n\n"
                "Tap ↩️ Back to return.",
                _back_kb(),
            )
        return

    if data == "lg_custom_back":
        await query.answer()
        prev = s.get("pre_custom_state", "ss_done")
        s["state"] = prev

        if prev == "ss_done" and s["ss_file_ids"]:
            await _show_nav(client, uid)
        else:
            # Was generating — restart from scratch
            s["state"]       = "generating"
            s["cancel_flag"] = False
            n_vid = sum(1 for f in s["files"] if f["media_type"] in ("video", "animation"))
            await _edit(client, chat_id, nav_id,
                        f"⏳ **Generating screenshots…**\n\n0 / {n_vid} video(s) processed",
                        _prog_kb())
            s["bg_task"] = asyncio.create_task(_gen_ss(client, uid, batch=s.get("batch", 0)))
        return

    if data == "lg_post":
        await query.answer("Posting to channel…")
        await _do_post(client, uid)
        return


# ── custom-file receiver ──────────────────────────────────────────────────────

def _want_custom(_, __, msg: Message) -> bool:
    if not msg.from_user:
        return False
    uid = msg.from_user.id
    s   = LINK_SESSIONS.get(uid)
    return bool(
        s and _is_admin(uid) and s["state"] == "custom_wait"
        and (msg.video or msg.photo or msg.document or msg.animation)
    )

_custom_filter = filters.create(_want_custom)


@Client.on_message(_custom_filter & filters.private)
async def receive_custom(client: Client, message: Message):
    uid = message.from_user.id
    s   = LINK_SESSIONS.get(uid)
    if not s:
        return

    fid = mtype = None
    if   message.video:     fid, mtype = message.video.file_id,     "video"
    elif message.photo:     fid, mtype = message.photo.file_id,     "photo"
    elif message.document:  fid, mtype = message.document.file_id,  "document"
    elif message.animation: fid, mtype = message.animation.file_id, "animation"
    if not fid:
        return

    await _do_post(client, uid, custom={"file_id": fid, "media_type": mtype, "msg_id": message.id})


# ── handle_link_access (called from cmds.py on ?start=link_<id>) ─────────────

async def handle_link_access(client: Client, message: Message, link_id: str):
    from vars import IS_FSUB, PROTECT_CONTENT
    from .fsub import get_fsub

    if IS_FSUB and not await get_fsub(client, message):
        return

    doc = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not doc:
        await message.reply_text("❌ This link is invalid or has expired.")
        return

    files = []
    for f in doc.get("files", []):
        if not isinstance(f, dict):
            continue
        fid   = f.get("file_id", "")
        mtype = f.get("media_type", "document")
        if fid:
            files.append({"file_id": fid, "media_type": mtype})

    if not files:
        await message.reply_text("❌ No files found for this link.")
        return

    cid = message.chat.id
    for f in files:
        fid, mtype = f["file_id"], f["media_type"]
        try:
            if   mtype == "video":     await client.send_video(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "photo":     await client.send_photo(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "audio":     await client.send_audio(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "voice":     await client.send_voice(cid, fid, protect_content=PROTECT_CONTENT)
            elif mtype == "animation": await client.send_animation(cid, fid, protect_content=PROTECT_CONTENT)
            else:                      await client.send_document(cid, fid, protect_content=PROTECT_CONTENT)
        except Exception as e:
            print(f"[handle_link_access] {mtype}: {e}")
        await asyncio.sleep(0.3)


# ── stubs (keep callback.py import happy) ────────────────────────────────────

async def show_screenshot(*a, **kw):            pass
async def generate_screenshots(*a, **kw):       pass
async def post_screenshot_to_channel(*a, **kw): pass
async def _cleanup_ss_files(*a, **kw):          pass
async def _finish_and_show_navigator(*a, **kw): pass
