# link_generator.py  —  /l  and  /m_link  admin flow
#
# FLOW
# ────
# /l
#   Reset session. Prompt shows live file count.
#   Every file sent → delete old prompt, send fresh one below.
#   Prompt includes the correct  /m_link  command.
#
# /m_link
#   Download each video. Use ffmpeg to extract real random screenshots:
#     1 video  → 10 ss spread evenly across full duration
#     N videos → ceil(10/N) ss per video  (total ≥ 10, all videos covered)
#   Live progress message while working.
#
# SS navigator (photo message as nav, caption = status)
#   [⬅️]  [3/10]  [➡️]
#   [🖼 Custom]   [♻️ More SS]
#   [📤 Post]     [❌ Cancel]
#
# POST format
#   Single file  → send actual file to channel
#       caption:  🔗 Link + 🆔 File ID
#   Multi file   → send selected SS / custom thumb to channel
#       caption:  🔗 Link + 🆔 Group ID
#   Always includes [🎬 Get File] button
#
# After posting
#   • NO message in admin DM
#   • Delete: nav msg, all collected file msgs, all SS photo msgs
#   • DB record stays → link is permanent
#   • /delete <file_id | group_id> removes from DB

from __future__ import annotations

import asyncio
import math
import os
import random
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


# ── tiny helpers ─────────────────────────────────────────────────────────────

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


# ── session store ─────────────────────────────────────────────────────────────
#
# LINK_SESSIONS[admin_id] = {
#   "chat_id"          : int
#   "state"            : "collecting" | "generating" | "ss_done" | "custom_wait"
#   "files"            : [{"file_id":str,"media_type":str,"msg_id":int}, ...]
#   "ask_msg_id"       : int | None   <- running "send files" prompt
#   "nav_msg_id"       : int | None   <- progress / navigator message
#   "ss_msgs"          : [{"file_id":str,"msg_id":int}, ...]  <- uploaded SS photos
#   "ss_index"         : int
#   "cancel_flag"      : bool
#   "bg_task"          : asyncio.Task | None
#   "pre_custom_state" : str
#   "batch"            : int          <- increments each "More SS" run
# }

LINK_SESSIONS: dict[int, dict] = {}

# compat names imported by callback.py
SCREENSHOT_SESSIONS   = LINK_SESSIONS
SS_CANCEL_FLAGS: dict = {}
SS_BG_TASKS: dict     = {}
SS_DL_CUSTOM_ACTIVE: dict = {}


def _new_sess(uid: int, chat_id: int) -> dict:
    return {
        "chat_id":           chat_id,
        "state":             "collecting",
        "files":             [],
        "ask_msg_id":        None,
        "nav_msg_id":        None,
        "ss_msgs":           [],
        "ss_index":          0,
        "cancel_flag":       False,
        "bg_task":           None,
        "pre_custom_state":  "generating",
        "batch":             0,
    }


def _kill(uid: int):
    s = LINK_SESSIONS.pop(uid, None)
    if not s:
        return
    s["cancel_flag"] = True
    t = s.get("bg_task")
    if t and not t.done():
        t.cancel()


# ── markups ───────────────────────────────────────────────────────────────────

def _ask_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
    ])


def _prog_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="lg_cancel")],
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
        [InlineKeyboardButton("🖼 Custom",  callback_data="lg_custom"),
         InlineKeyboardButton("♻️ More SS", callback_data="lg_more")],
        [InlineKeyboardButton("📤 Post",    callback_data="lg_post"),
         InlineKeyboardButton("❌ Cancel",  callback_data="lg_cancel")],
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


# ── /l command ────────────────────────────────────────────────────────────────

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


# ── file collector ────────────────────────────────────────────────────────────

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
    n = len(s["files"])

    if s["ask_msg_id"]:
        await _del(client, message.chat.id, s["ask_msg_id"])
    ask = await message.reply_text(_ask_text(n), reply_markup=_ask_kb())
    s["ask_msg_id"] = ask.id


# ── /m_link command ───────────────────────────────────────────────────────────

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

    await _del(client, message.chat.id, message.id)
    if s["ask_msg_id"]:
        await _del(client, message.chat.id, s["ask_msg_id"])
        s["ask_msg_id"] = None

    s["state"] = "generating"
    n_vid = sum(1 for f in s["files"] if f["media_type"] in ("video", "animation"))
    nav = await client.send_message(
        message.chat.id,
        f"⏳ **Generating screenshots…**\n\n"
        f"0 / {n_vid} video(s) processed",
        reply_markup=_prog_kb(),
    )
    s["nav_msg_id"] = nav.id
    s["bg_task"] = asyncio.create_task(_gen_ss(client, uid, batch=0))


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

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
    """Evenly spread `count` timestamps across [2%, 98%] of the video.
    Each batch shifts the grid by ~half a step so More SS gives different frames."""
    if dur <= 0:
        return [float(i + 1) for i in range(count)]
    lo   = dur * 0.02
    hi   = dur * 0.98
    span = hi - lo
    step = span / count
    shift = (batch * step * 0.47) % span
    ts = [lo + (i * step + shift) % span for i in range(count)]
    random.shuffle(ts)
    return [round(t, 3) for t in ts]


# ── main SS generation task ───────────────────────────────────────────────────

SS_COUNT = 10   # screenshots per run


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
                    "⚠️ No video files detected.\n\n"
                    "Use 🖼 Custom to pick a thumbnail manually, then 📤 Post.",
                    _back_kb())
        return

    ss_per_vid = math.ceil(SS_COUNT / n_vids)   # ensures total >= SS_COUNT
    new_ss: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="tgss_") as tmp:
        for vi, vf in enumerate(videos):
            if s.get("cancel_flag"):
                return

            # progress
            await _edit(client, chat_id, nav_id,
                        f"⏳ **Generating screenshots…**\n\n"
                        f"Downloading file {vi + 1} / {n_vids}…",
                        _prog_kb())

            # download
            dl_path = os.path.join(tmp, f"v{vi}.mp4")
            try:
                dl_path = await client.download_media(vf["file_id"], file_name=dl_path)
            except Exception as e:
                print(f"[lg] download err: {e}")
                continue
            if not dl_path or not os.path.exists(dl_path):
                continue
            if s.get("cancel_flag"):
                return

            # duration
            dur = await _duration(dl_path)

            await _edit(client, chat_id, nav_id,
                        f"⏳ **Generating screenshots…**\n\n"
                        f"File {vi + 1} / {n_vids}  —  extracting {ss_per_vid} frames…",
                        _prog_kb())

            # extract all frames in parallel
            ts_list  = _timestamps(dur, ss_per_vid, batch)
            ss_dir   = os.path.join(tmp, f"ss{vi}")
            os.makedirs(ss_dir, exist_ok=True)
            out_paths = [os.path.join(ss_dir, f"{i:03d}.jpg") for i in range(len(ts_list))]
            await asyncio.gather(
                *[_grab_frame(dl_path, ts, op) for ts, op in zip(ts_list, out_paths)]
            )
            if s.get("cancel_flag"):
                return

            # upload SS photos to admin DM
            for op in out_paths:
                if s.get("cancel_flag"):
                    return
                if not os.path.exists(op) or os.path.getsize(op) == 0:
                    continue
                try:
                    sent = await client.send_photo(chat_id, op)
                    new_ss.append({"file_id": sent.photo.file_id, "msg_id": sent.id})
                except Exception as e:
                    print(f"[lg] upload err: {e}")
                await asyncio.sleep(0.05)  # yield – don't starve other handlers

            # free disk immediately
            try:
                os.remove(dl_path)
            except Exception:
                pass

    if s.get("cancel_flag"):
        return

    old_n          = len(s["ss_msgs"])
    s["ss_msgs"].extend(new_ss)
    s["ss_index"]  = old_n if new_ss else max(0, old_n - 1)
    s["batch"]     = batch + 1
    s["state"]     = "ss_done"

    if not s["ss_msgs"]:
        await _edit(client, chat_id, nav_id,
                    "⚠️ Could not extract any screenshots.\n\n"
                    "Use 🖼 Custom to pick a thumbnail manually.",
                    _back_kb())
        return

    await _show_nav(client, uid)


# ── show SS navigator ─────────────────────────────────────────────────────────

async def _show_nav(client: Client, uid: int):
    s       = LINK_SESSIONS.get(uid)
    if not s:
        return
    ss      = s["ss_msgs"]
    idx     = s["ss_index"]
    total   = len(ss)
    chat_id = s["chat_id"]
    nav_id  = s["nav_msg_id"]
    if not ss or idx >= total:
        return

    item    = ss[idx]
    caption = f"🖼 **Screenshot  {idx + 1} / {total}**"
    markup  = _nav_kb(idx, total)

    try:
        await client.edit_message_media(
            chat_id, nav_id,
            InputMediaPhoto(media=item["file_id"], caption=caption),
            reply_markup=markup,
        )
    except Exception:
        # fallback: delete nav, send new photo as nav
        await _del(client, chat_id, nav_id)
        try:
            new = await client.send_photo(
                chat_id, item["file_id"],
                caption=caption, reply_markup=markup,
            )
            s["nav_msg_id"] = new.id
        except Exception as e:
            print(f"[lg] nav send err: {e}")


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

    # save to DB
    link_id = _rand_id(10)
    single  = len(files) == 1
    post_id = files[0]["file_id"][:20] if single else _rand_id(12)
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
    get_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Get File", url=tg_link)]
    ])

    if single:
        caption = f"🔗 **Link:** `{tg_link}`\n\n🆔 **File ID:** `{post_id}`"
    else:
        caption = f"🔗 **Link:** `{tg_link}`\n\n🆔 **Group ID:** `{post_id}`"

    # choose what to send to channel
    if custom:
        pfid, pmtype = custom["file_id"], custom["media_type"]
    elif single:
        pfid, pmtype = files[0]["file_id"], files[0]["media_type"]
    elif s["ss_msgs"]:
        item  = s["ss_msgs"][s["ss_index"]]
        pfid, pmtype = item["file_id"], "photo"
    else:
        pfid, pmtype = files[0]["file_id"], files[0]["media_type"]

    # post
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

    # delete all admin DM messages silently
    to_del: list[int] = []
    if nav_id:
        to_del.append(nav_id)
    for f in files:
        if f.get("msg_id"):
            to_del.append(f["msg_id"])
    for ss in s["ss_msgs"]:
        if ss.get("msg_id"):
            to_del.append(ss["msg_id"])
    if custom and custom.get("msg_id"):
        to_del.append(custom["msg_id"])

    for i in range(0, len(to_del), 100):
        try:
            await client.delete_messages(chat_id, to_del[i:i + 100])
        except Exception:
            pass
        await asyncio.sleep(0.05)

    _kill(uid)   # no DM message — silent clean-up


# ── callback dispatcher (called from callback.py) ────────────────────────────

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
            for ss in s.get("ss_msgs", []):
                if ss.get("msg_id"):
                    to_del.append(ss["msg_id"])
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
        if s["ss_index"] < len(s["ss_msgs"]) - 1:
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
            await _edit(client, chat_id, nav_id,
                        "📎 **Send a photo or video to use as the post thumbnail.**\n\n"
                        "Tap ↩️ Back to return.",
                        _back_kb())
        return

    if data == "lg_custom_back":
        await query.answer()
        prev       = s.get("pre_custom_state", "ss_done")
        s["state"] = prev
        if prev == "ss_done" and s["ss_msgs"]:
            await _show_nav(client, uid)
        else:
            await _edit(client, chat_id, nav_id,
                        "⏳ **Still generating… please wait.**",
                        _prog_kb())
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

    s["cancel_flag"] = True
    t = s.get("bg_task")
    if t and not t.done():
        t.cancel()
    s["cancel_flag"] = False

    await _do_post(client, uid, custom={"file_id": fid, "media_type": mtype, "msg_id": message.id})


# ── handle_link_access  (called from cmds.py on ?start=link_<id>) ────────────

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
