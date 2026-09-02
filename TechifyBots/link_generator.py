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
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation,
    Message,
)

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
        "files":             [],          # list of {"file_id", "media_type", "msg_id"}
        "collage_groups":    [],          # list of lists — each inner list = one media-group
        "pending_group":     {},          # {media_group_id: [items...]} for debouncing
        "ask_msg_id":        None,
        "nav_msg_id":        None,
        "ss_paths":          [],
        "ss_tmp_dir":        None,
        "ss_index":          0,
        "cancel_flag":       False,
        "bg_task":           None,
        "batch":             0,
        "pre_custom_state":  "generating",
        "_collect_lock":     asyncio.Lock(),
        "_settle_task":      None,
        "_group_flush_tasks": {},         # {media_group_id: asyncio.Task}
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
    for t in s.get("_group_flush_tasks", {}).values():
        if t and not t.done():
            t.cancel()
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
    row: list[InlineKeyboardButton] = [
        InlineKeyboardButton("⬅️", callback_data="lg_prev"),
        InlineKeyboardButton(f"{idx + 1}/{total}", callback_data="lg_noop"),
        InlineKeyboardButton("➡️", callback_data="lg_next"),
    ]
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


_CUSTOM_PROMPT = (
    "📎 **Send a photo or video to use as the post thumbnail.**\n\n"
    "Tap ❌ Cancel to cancel."
)


async def _enter_custom_wait_direct(client: Client, uid: int):
    """
    Skip the "no screenshots available" notice entirely and drop the admin
    straight into the custom-thumbnail prompt, with a Cancel button (there is
    nothing meaningful to go "back" to, since no screenshots exist).
    """
    s = LINK_SESSIONS.get(uid)
    if not s:
        return
    s["pre_custom_state"] = "collecting"
    s["state"] = "custom_wait"
    await _edit(client, s["chat_id"], s["nav_msg_id"], _CUSTOM_PROMPT, _ask_kb())


# ── ask-text ──────────────────────────────────────────────────────────────────

def _ask_text(n: int, n_groups: int = 0) -> str:
    word = "file" if n == 1 else "files"
    group_info = f" ({n_groups} collage group{'s' if n_groups != 1 else ''})" if n_groups > 0 else ""
    return (
        f"📁 **Link Generator**\n\n"
        f"Files received: **{n} {word}**{group_info}\n\n"
        f"Send any file (video · photo · audio · document …)\n"
        f"Send multiple photos/videos together for a **collage**.\n\n"
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

    file_entry = {"file_id": fid, "media_type": mtype, "msg_id": message.id}

    media_group_id = getattr(message, "media_group_id", None)

    if media_group_id:
        # ── This message is part of a media group (collage) ──
        group = s["pending_group"].setdefault(media_group_id, [])
        group.append(file_entry)

        # Cancel any existing flush task for this group and restart the timer
        existing = s["_group_flush_tasks"].get(media_group_id)
        if existing and not existing.done():
            existing.cancel()
        s["_group_flush_tasks"][media_group_id] = asyncio.create_task(
            _flush_group(client, uid, media_group_id)
        )
    else:
        # ── Single file (not part of a collage) ──
        s["files"].append(file_entry)

        # Cancel and restart the settle task
        old = s.get("_settle_task")
        if old and not old.done():
            old.cancel()
        s["_settle_task"] = asyncio.create_task(
            _settle_prompt(client, uid, message.chat.id)
        )


async def _flush_group(client: Client, uid: int, media_group_id: str):
    """
    Wait a short time for all messages in the media group to arrive,
    then commit the whole group as a single collage entry.
    """
    await asyncio.sleep(1.0)   # Telegram delivers group messages within ~0.5–0.8 s
    s = LINK_SESSIONS.get(uid)
    if not s or s["state"] != "collecting":
        return

    group_items = s["pending_group"].pop(media_group_id, [])
    if not group_items:
        return

    # Store as collage group (preserves ordering within group)
    s["collage_groups"].append(group_items)
    # Also add all items to the flat file list (used for SS generation etc.)
    s["files"].extend(group_items)

    # Update the prompt
    old = s.get("_settle_task")
    if old and not old.done():
        old.cancel()
    s["_settle_task"] = asyncio.create_task(
        _settle_prompt(client, uid, s["chat_id"])
    )


async def _settle_prompt(client: Client, uid: int, chat_id: int):
    """Wait for the burst to settle, then update the prompt once."""
    await asyncio.sleep(SETTLE_DELAY)
    s = LINK_SESSIONS.get(uid)
    if not s or s["state"] != "collecting":
        return

    n        = len(s["files"])
    n_groups = len(s["collage_groups"])
    if s["ask_msg_id"]:
        await _del(client, chat_id, s["ask_msg_id"])
        s["ask_msg_id"] = None

    try:
        ask = await client.send_message(chat_id, _ask_text(n, n_groups), reply_markup=_ask_kb())
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

    # Flush any pending group that hasn't been committed yet
    for gid, task in list(s.get("_group_flush_tasks", {}).items()):
        if task and not task.done():
            task.cancel()
        await _flush_group(client, uid, gid)

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
        # No videos to screenshot (e.g. photos-only post) — go straight to
        # asking for a custom thumbnail instead of showing an interstitial
        # notice with a "Custom" button the admin has to tap separately.
        await _enter_custom_wait_direct(client, uid)
        return

    ss_per_vid = math.ceil(SS_COUNT / n_vids)

    if not s.get("ss_tmp_dir") or not os.path.isdir(s["ss_tmp_dir"]):
        s["ss_tmp_dir"] = tempfile.mkdtemp(prefix="tgss_")
    tmp = s["ss_tmp_dir"]

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
            await _enter_custom_wait_direct(client, uid)
            return

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
        await _enter_custom_wait_direct(client, uid)
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

    ss_cache: dict = s.setdefault("ss_file_id_cache", {})
    cached_fid = ss_cache.get(path)

    try:
        if cached_fid:
            await client.edit_message_media(
                chat_id, nav_id,
                InputMediaPhoto(media=cached_fid, caption=caption),
                reply_markup=markup,
            )
        else:
            try:
                new_msg = await client.edit_message_media(
                    chat_id, nav_id,
                    InputMediaPhoto(media=path, caption=caption),
                    reply_markup=markup,
                )
                if new_msg and new_msg.photo:
                    ss_cache[path] = new_msg.photo.file_id
            except Exception:
                await _del(client, chat_id, nav_id)
                new = await client.send_photo(
                    chat_id, path, caption=caption, reply_markup=markup,
                )
                s["nav_msg_id"] = new.id
                if new.photo:
                    ss_cache[path] = new.photo.file_id
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

def _make_input_media_for_post(fid: str, mtype: str):
    """Build an InputMedia* object with NO caption (captions stripped from collage items)."""
    if mtype == "photo":
        return InputMediaPhoto(media=fid)
    elif mtype in ("video", "animation"):
        return InputMediaVideo(media=fid)
    elif mtype == "document":
        return InputMediaDocument(media=fid)
    elif mtype == "audio":
        return InputMediaAudio(media=fid)
    else:
        return InputMediaDocument(media=fid)


def _media_bucket(mtype: str) -> str:
    """
    Telegram's sendMediaGroup only allows grouping photos+videos together,
    or documents together, or audio together — it rejects a call that mixes
    those categories. Bucket every file so runs of compatible types can be
    grouped into one collage, regardless of whether Telegram happened to
    tag them with a shared media_group_id (mixed selections — e.g. photo +
    gif + video sent together — don't always get one).
    """
    if mtype in ("photo", "video", "animation"):
        return "media"
    if mtype == "audio":
        return "audio"
    return "document"   # documents, voice notes, anything else


def _chunk(seq: list, n: int) -> list:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


async def _post_files_as_collage(client: Client, files: list,
                                  get_btn: InlineKeyboardMarkup, post_id: str) -> list[int]:
    """
    Post 2+ files to POST_CHANNEL as one or more media-group "collages",
    preserving the order the admin sent them in. Runs of Telegram-compatible
    types are grouped together (max 10 per album, per Bot API limits); a
    lone file that can't be grouped (run of 1, or a >10 overflow remainder)
    is sent as a normal single message instead, since sendMediaGroup
    requires 2-10 items. The link button + Post ID is attached to the very
    last message sent. Raises on failure so the caller can report it.
    """
    # Build ordered runs of same-bucket files, then split each run into
    # chunks of at most 10 (Telegram's media-group cap).
    runs: list[list[dict]] = []
    for f in files:
        b = _media_bucket(f["media_type"])
        if runs and _media_bucket(runs[-1][-1]["media_type"]) == b:
            runs[-1].append(f)
        else:
            runs.append([f])

    chunks: list[list[dict]] = []
    for run in runs:
        chunks.extend(_chunk(run, 10))

    posted_msg_ids: list[int] = []
    last_msg: Optional[Message] = None

    for ci, chunk in enumerate(chunks):
        is_last_chunk = (ci == len(chunks) - 1)

        if len(chunk) == 1:
            f = chunk[0]
            if is_last_chunk:
                sent = await _post_single(client, f["file_id"], f["media_type"], None, get_btn, post_id=post_id)
            else:
                sent = await _post_single_raw(client, f["file_id"], f["media_type"])
            if sent:
                last_msg = sent
                posted_msg_ids.append(sent.id)
        else:
            media_list = [_make_input_media_for_post(f["file_id"], f["media_type"]) for f in chunk]
            msgs = await client.send_media_group(POST_CHANNEL, media_list)
            if msgs:
                last_msg = msgs[-1]
                posted_msg_ids.extend(m.id for m in msgs)
            if is_last_chunk and msgs:
                btn_msg = await client.send_message(
                    POST_CHANNEL,
                    f">🆔 Post ID : `{post_id}`",
                    reply_markup=get_btn,
                    reply_to_message_id=msgs[-1].id,
                )
                if btn_msg:
                    posted_msg_ids.append(btn_msg.id)

        if not is_last_chunk:
            await asyncio.sleep(0.3)

    return posted_msg_ids


async def _do_post(client: Client, uid: int, custom: Optional[dict] = None):
    s = LINK_SESSIONS.get(uid)
    if not s:
        return

    chat_id       = s["chat_id"]
    nav_id        = s["nav_msg_id"]
    files         = s["files"]
    collage_groups = s.get("collage_groups", [])

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

    get_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Get Video", url=tg_link)]
    ])

    # Tracks every message id we post to POST_CHANNEL for this post, so the
    # /delete command can later remove the actual channel post(s) as well as
    # the DB record — not just the DB record.
    posted_msg_ids: list[int] = []

    # ── Choose preview / post strategy ───────────────────────────────────
    if custom:
        # Admin picked a custom thumbnail → use it as a single photo post
        pfid, pmtype = custom["file_id"], custom["media_type"]
        use_path = None
        sent = await _post_single(client, pfid, pmtype, use_path, get_btn, post_id=post_id)
        if not sent:
            # BUGFIX: previously the return value of _post_single was never
            # checked here, so if sending to POST_CHANNEL failed (wrong
            # POST_CHANNEL id, bot not admin there, invalid file_id, etc.)
            # the error was swallowed silently and the code fell straight
            # through to the "success" cleanup below — deleting the admin's
            # messages and resetting the session even though nothing was
            # ever posted. Now we surface the failure and stop before
            # cleaning up, exactly like every other posting path below.
            await _edit(client, chat_id, nav_id, "⚠️ **Post failed.** Check bot logs / POST_CHANNEL permissions.", None)
            return
        posted_msg_ids.append(sent.id)

    elif len(files) >= 2:
        # 2+ files → always post as a collage in the channel, in the order
        # the admin sent them, regardless of whether Telegram tagged them
        # with a shared media_group_id (mixed selections like photo + gif +
        # video don't always get one). Compatible runs (photo/video/anim
        # together, documents together, audio together) are grouped into
        # media-group albums; anything that can't be grouped is sent as a
        # normal message so nothing is silently dropped.
        try:
            ids = await _post_files_as_collage(client, files, get_btn, post_id)
            posted_msg_ids.extend(ids)
            if not ids:
                await _edit(client, chat_id, nav_id, "⚠️ **Post failed.** Check bot logs / POST_CHANNEL permissions.", None)
                return
        except Exception as err:
            await _edit(client, chat_id, nav_id, f"⚠️ **Post failed.**\n\nError: `{err}`", None)
            return

    else:
        # ── Original single-file path ─────────────────────────────────────
        if s["ss_paths"]:
            ss_cache: dict = s.get("ss_file_id_cache", {})
            current_path   = s["ss_paths"][s["ss_index"]]
            cached_fid     = ss_cache.get(current_path)
            if cached_fid:
                use_path = None
                pfid, pmtype = cached_fid, "photo"
            else:
                use_path = current_path
                pfid, pmtype = None, "photo"
        elif single:
            pfid, pmtype = files[0]["file_id"], files[0]["media_type"]
            use_path = None
        else:
            pfid, pmtype = files[0]["file_id"], files[0]["media_type"]
            use_path = None

        sent = await _post_single(client, pfid, pmtype, use_path, get_btn, post_id=post_id)
        if not sent:
            await _edit(client, chat_id, nav_id, "⚠️ **Post failed.**", None)
            return
        posted_msg_ids.append(sent.id)

    # ── Persist the POST_CHANNEL message id(s) so /delete can remove the
    #    actual channel post later, not just the DB record ─────────────────
    if posted_msg_ids:
        try:
            await mdb.async_db["file_links"].update_one(
                {"link_id": link_id},
                {"$set": {"channel_msg_ids": posted_msg_ids, "post_channel_id": POST_CHANNEL}}
            )
        except Exception as e:
            print(f"[lg] failed to persist channel_msg_ids: {e}")

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

    tmp = s.get("ss_tmp_dir")
    if tmp and os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)

    _kill(uid)


async def _post_single(client: Client, fid: str, mtype: str,
                       use_path, markup: InlineKeyboardMarkup,
                       post_id: str = "") -> Optional[Message]:
    """Send a single file to POST_CHANNEL with the link button and Post ID caption."""
    # BUGFIX: post_id used to be accepted but silently dropped here, so
    # single-file posts had no Post ID caption at all (unlike collage posts,
    # which show it in a follow-up message). Restore it as the post caption.
    caption = f">🆔 Post ID : `{post_id}`" if post_id else None
    try:
        if mtype == "photo":
            media = use_path if use_path else fid
            return await client.send_photo(POST_CHANNEL, media, caption=caption, reply_markup=markup)
        elif mtype in ("video", "animation"):
            return await client.send_video(POST_CHANNEL, fid, caption=caption, reply_markup=markup)
        elif mtype == "document":
            return await client.send_document(POST_CHANNEL, fid, caption=caption, reply_markup=markup)
        elif mtype == "audio":
            return await client.send_audio(POST_CHANNEL, fid, caption=caption, reply_markup=markup)
        else:
            return await client.send_document(POST_CHANNEL, fid, caption=caption, reply_markup=markup)
    except Exception as e:
        print(f"[lg] _post_single error: {e}")
        return None


async def _post_single_raw(client: Client, fid: str, mtype: str) -> Optional[Message]:
    """Send a single file to POST_CHANNEL with no caption and no button."""
    try:
        if mtype == "photo":
            return await client.send_photo(POST_CHANNEL, fid)
        elif mtype in ("video", "animation"):
            return await client.send_video(POST_CHANNEL, fid)
        elif mtype == "document":
            return await client.send_document(POST_CHANNEL, fid)
        elif mtype == "audio":
            return await client.send_audio(POST_CHANNEL, fid)
        else:
            return await client.send_document(POST_CHANNEL, fid)
    except Exception as e:
        print(f"[lg] _post_single_raw error: {e}")
        return None


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
        total = len(s["ss_paths"])
        if total:
            s["ss_index"] = (s["ss_index"] - 1) % total
            await _show_nav(client, uid)
        return

    if data == "lg_next":
        await query.answer()
        total = len(s["ss_paths"])
        if total:
            s["ss_index"] = (s["ss_index"] + 1) % total
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

async def handle_link_access(client: Client, message: Message, link_id: str):
    from .fsub import get_fsub
    from Database.userdb import udb
    from .cmds import (
        get_cached_user_data, get_cached_verification,
        _make_file_buttons, show_verify, USER_CURRENT_VIDEO,
        USER_ACTIVE_VIDEOS, auto_delete, clear_user_cache,
        _push_to_history_cache, _get_history_cache,
    )

    uid = message.from_user.id
    cid = message.chat.id

    # ── Animated checking placeholder ────────────────────────────────────
    anim_frames = ["!", "!!", "!!!", "?", "??", "???"]
    anim_msg = await message.reply_text(anim_frames[0])
    async def _animate(stop_event: asyncio.Event):
        i = 1
        while not stop_event.is_set():
            try:
                await anim_msg.edit_text(anim_frames[i % len(anim_frames)])
            except Exception:
                pass
            i += 1
            await asyncio.sleep(0.4)
    stop_anim = asyncio.Event()
    anim_task = asyncio.create_task(_animate(stop_anim))

    async def _stop_and_edit(text: str, markup=None):
        stop_anim.set()
        anim_task.cancel()
        try:
            await anim_msg.edit_text(text, reply_markup=markup)
        except Exception:
            pass

    # ── Ban check ─────────────────────────────────────────────────────────
    if await udb.is_user_banned(uid):
        await _stop_and_edit("**🚫 You are banned from using this bot**")
        return

    # ── Force-sub check ───────────────────────────────────────────────────
    bot_settings = await mdb.get_bot_settings()
    if bot_settings["is_fsub"] and not await get_fsub(client, message, start_param=f"link_{link_id}"):
        stop_anim.set()
        anim_task.cancel()
        try:
            await anim_msg.delete()
        except Exception:
            pass
        return

    # ── Maintenance check ─────────────────────────────────────────────────
    limits = await mdb.get_global_limits()
    if limits.get("maintenance"):
        await _stop_and_edit("**🛠️ Bot Under Maintenance — Back Soon!**")
        return

    # ── Fetch link document ───────────────────────────────────────────────
    doc = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    if not doc:
        await _stop_and_edit("❌ This link is invalid or has expired.")
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
        await _stop_and_edit("❌ No files found for this link.")
        return

    # ── Usage / limit / verification check (same as send_video) ──────────
    user     = await get_cached_user_data(uid)
    is_prime = user.get("plan") == "prime"
    usage_text = ""

    if is_prime:
        usage_text = "🌟 User Plan : Prime"
        protect = False if bot_settings["premium_can_download"] else bot_settings["protect_content"]
    else:
        protect = bot_settings["protect_content"]
        if bot_settings["is_verify"]:
            verified, is_second, is_third = await get_cached_verification(uid)
            if verified and not is_second and not is_third:
                usage_text = "**Status : ✅ Verified**"
            else:
                usage = await mdb.check_and_increment_usage(uid)
                if usage["allowed"]:
                    usage_text = f"📊 Daily Limit : {usage['count']}/{usage['limit']}"
                else:
                    stop_anim.set()
                    anim_task.cancel()
                    try:
                        await anim_msg.delete()
                    except Exception:
                        pass
                    await show_verify(client, message, uid, is_second, is_third)
                    return
        else:
            # Verification system is OFF → free daily-limit is OFF too.
            usage_text = "📊 Access : Unlimited"

    # Animation done — delete it before sending files
    stop_anim.set()
    anim_task.cancel()
    try:
        await anim_msg.delete()
    except Exception:
        pass

    # ── Send files ────────────────────────────────────────────────────────
    mins = DELETE_TIMER // 60
    sent_msg_ids = []
    last_idx = len(files) - 1

    for idx, f in enumerate(files):
        fid, mtype = f["file_id"], f["media_type"]
        is_last    = (idx == last_idx)

        if is_last:
            current_history  = _get_history_cache(uid)
            has_previous     = len(current_history) > 0
            caption  = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}</b>"
            buttons  = _make_file_buttons(uid, has_previous)
            markup   = InlineKeyboardMarkup(buttons)
            USER_CURRENT_VIDEO[uid] = fid
        else:
            caption = None
            markup  = None

        try:
            kwargs = dict(protect_content=protect, reply_markup=markup)
            if caption:
                kwargs["caption"] = caption
            if   mtype == "video":
                sent = await client.send_video(cid, fid, **kwargs)
            elif mtype == "photo":
                sent = await client.send_photo(cid, fid, **kwargs)
            elif mtype == "audio":
                sent = await client.send_audio(cid, fid, **kwargs)
            elif mtype == "voice":
                sent = await client.send_voice(cid, fid, **kwargs)
            elif mtype == "animation":
                sent = await client.send_animation(cid, fid, **kwargs)
            else:
                sent = await client.send_document(cid, fid, **kwargs)

            sent_msg_ids.append(sent.id)

            if is_last:
                _push_to_history_cache(uid, fid, mtype)
                asyncio.create_task(mdb.add_to_watch_history(uid, fid, mtype))
                USER_ACTIVE_VIDEOS.setdefault(uid, set()).add(sent.id)
                asyncio.create_task(auto_delete(client, cid, sent.id, uid))

        except Exception as e:
            print(f"[handle_link_access] {mtype}: {e}")
        await asyncio.sleep(0.3)

    if len(sent_msg_ids) > 1:
        asyncio.create_task(_delete_link_files(client, cid, sent_msg_ids[:-1]))


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
