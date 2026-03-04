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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,Message

from vars import ADMIN_IDS, POST_CHANNEL, DELETE_TIMER
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
        "ss_paths":          [],   # on-disk paths (no DM upload)
        "ss_tmp_dir":        None,
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
    # Clean up any leftover temp directory
    tmp = s.get("ss_tmp_dir")
    if tmp and os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)


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
        f"Send /m_link when done to generate screenshots & link."
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

    s["files"].append({"file_id": fid, "media_type": mtype, "msg_id": message.id})

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
    if s["ask_msg_id"]:
        await _del(client, chat_id, s["ask_msg_id"])
        s["ask_msg_id"] = None

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

    # Reuse or create a persistent temp dir for this session's screenshots
    if not s.get("ss_tmp_dir") or not os.path.isdir(s["ss_tmp_dir"]):
        s["ss_tmp_dir"] = tempfile.mkdtemp(prefix="tgss_")
    tmp = s["ss_tmp_dir"]

    # Keep a sub-folder per batch so paths never collide
    batch_dir = os.path.join(tmp, f"batch{batch}")
    os.makedirs(batch_dir, exist_ok=True)

    collected_paths: list[str] = []

    try:
        for vi, vf in enumerate(videos):
            if s.get("cancel_flag"):
                return

            await _edit(client, chat_id, nav_id,
                        f"⏳ **Generating screenshots…**\n\n"
                        f"Downloading file {vi + 1} / {n_vids}…",
                        _prog_kb())

            dl_path = os.path.join(batch_dir, f"v{vi}.mp4")
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
            ss_dir  = os.path.join(batch_dir, f"ss{vi}")
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

        # ── Store paths in session — NO DM upload, NO delete ──────────────
        old_n = len(s["ss_paths"])
        s["ss_paths"].extend(collected_paths)

    except Exception as e:
        print(f"[lg] _gen_ss error: {e}")
        return

    if s.get("cancel_flag"):
        return

    s["ss_index"] = old_n if s["ss_paths"][old_n:] else max(0, old_n - 1)
    s["batch"]    = batch + 1
    s["state"]    = "ss_done"

    if not s["ss_paths"]:
        await _edit(client, chat_id, nav_id,
                    "⚠️ Could not extract any screenshots.\n\nUse 🖼 Custom to pick a thumbnail.",
                    _back_kb())
        return

    await _show_nav(client, uid)


# ── show navigator ────────────────────────────────────────────────────────────

async def _show_nav(client: Client, uid: int):
    s = LINK_SESSIONS.get(uid)
    if not s:
        return
    paths   = s["ss_paths"]
    idx     = s["ss_index"]
    total   = len(paths)
    chat_id = s["chat_id"]
    nav_id  = s["nav_msg_id"]
    if not paths or idx >= total:
        return

    path    = paths[idx]
    caption = f"🖼 **Screenshot  {idx + 1} / {total}**"
    markup  = _nav_kb(idx, total)

    try:
        await client.edit_message_media(
            chat_id, nav_id,
            InputMediaPhoto(media=path, caption=caption),
            reply_markup=markup,
        )
    except Exception:
        await _del(client, chat_id, nav_id)
        try:
            new = await client.send_photo(
                chat_id, path, caption=caption, reply_markup=markup,
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

    caption = f">🆔 : `{post_id}`"

    get_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Get File", url=tg_link)]
    ])

    # ── Choose preview media ──────────────────────────────────────────────
    if custom:
        pfid, pmtype = custom["file_id"], custom["media_type"]
        use_path = None
    elif single:
        pfid, pmtype = files[0]["file_id"], files[0]["media_type"]
        use_path = None
    elif s["ss_paths"]:
        # Use on-disk path directly — no prior DM upload needed
        use_path = s["ss_paths"][s["ss_index"]]
        pfid, pmtype = None, "photo"
    else:
        pfid, pmtype = files[0]["file_id"], files[0]["media_type"]
        use_path = None

    # ── Send to channel ───────────────────────────────────────────────────
    try:
        if pmtype == "photo":
            media = use_path if use_path else pfid
            await client.send_photo(POST_CHANNEL, media, caption=caption, reply_markup=get_btn)
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

    # ── Clean up admin DM ─────────────────────────────────────────────────
    to_del: list[int] = []
    if nav_id:
        to_del.append(nav_id)
    for f in files:
        if f.get("msg_id"):
            to_del.append(f["msg_id"])
    if custom and custom.get("msg_id"):
        to_del.append(custom["msg_id"])

    for i in range(0, len(to_del), 100):
        try:
            await client.delete_messages(chat_id, to_del[i:i + 100])
        except Exception:
            pass
        await asyncio.sleep(0.05)

    # ── Clean up on-disk temp directory ──────────────────────────────────
    tmp = s.get("ss_tmp_dir")
    if tmp and os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)

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
        if s["ss_index"] < len(s["ss_paths"]) - 1:
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

        if prev == "ss_done" and s["ss_paths"]:
            await _show_nav(client, uid)
        else:
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
# Changes:
#   • "Get More Videos" button appended below the LAST sent file
#   • All sent messages are deleted after DELETE_TIMER seconds

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

    cid          = message.chat.id
    sent_msg_ids = []

    for idx, f in enumerate(files):
        fid, mtype = f["file_id"], f["media_type"]
        is_last    = (idx == len(files) - 1)

        # Add "Get More Videos" button only on the last file
        markup = None
        if is_last:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")]
            ])

        try:
            if   mtype == "video":
                sent = await client.send_video(cid, fid, protect_content=PROTECT_CONTENT, reply_markup=markup)
            elif mtype == "photo":
                sent = await client.send_photo(cid, fid, protect_content=PROTECT_CONTENT, reply_markup=markup)
            elif mtype == "audio":
                sent = await client.send_audio(cid, fid, protect_content=PROTECT_CONTENT, reply_markup=markup)
            elif mtype == "voice":
                sent = await client.send_voice(cid, fid, protect_content=PROTECT_CONTENT, reply_markup=markup)
            elif mtype == "animation":
                sent = await client.send_animation(cid, fid, protect_content=PROTECT_CONTENT, reply_markup=markup)
            else:
                sent = await client.send_document(cid, fid, protect_content=PROTECT_CONTENT, reply_markup=markup)

            sent_msg_ids.append(sent.id)
        except Exception as e:
            print(f"[handle_link_access] {mtype}: {e}")
        await asyncio.sleep(0.3)

    # Schedule deletion of all sent files after DELETE_TIMER seconds
    if sent_msg_ids:
        asyncio.create_task(_delete_link_files(client, cid, sent_msg_ids))


async def _delete_link_files(client: Client, cid: int, msg_ids: list[int]):
    """Delete link-provided files after DELETE_TIMER seconds."""
    await asyncio.sleep(DELETE_TIMER)
    for i in range(0, len(msg_ids), 100):
        try:
            await client.delete_messages(cid, msg_ids[i:i + 100])
        except Exception:
            pass
        await asyncio.sleep(0.05)


# ── stubs (keep callback.py import happy) ────────────────────────────────────

async def show_screenshot(*a, **kw):            pass
async def generate_screenshots(*a, **kw):       pass
async def post_screenshot_to_channel(*a, **kw): pass
async def _cleanup_ss_files(*a, **kw):          pass
async def _finish_and_show_navigator(*a, **kw): pass
