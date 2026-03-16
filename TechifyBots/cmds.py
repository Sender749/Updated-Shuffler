from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
from vars import *
from Database.maindb import mdb
from Database.userdb import udb
from datetime import datetime
import pytz, random, asyncio, string, time
from .fsub import get_fsub
from Script import text
from .utils import get_shortlink, get_readable_time
from bot import bot

# ==================== PERFORMANCE CACHES ====================
# Per-category caches: {category_name: (videos_list, timestamp)}
VIDEO_CACHE: dict = {}
VIDEO_CACHE_TTL = 300   # 5 minutes

USER_ACTIVE_VIDEOS = {}
USER_RECENT_VIDEOS = {}
TEMP_CHAT = {}

USER_DATA_CACHE = {}
USER_CACHE_TTL = 60

BOT_INFO_CACHE = None

VERIFICATION_CACHE = {}
VERIFICATION_CACHE_TTL = 60

USER_CURRENT_VIDEO = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==================== CATEGORY HELPERS ====================

def _build_category_markup(current_category: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with all categories, CATEGORY_BUTTONS_PER_ROW per row."""
    buttons = []
    row = []

    all_label = "✅ 🌐 All" if current_category == "all" else "🌐 All"
    row.append(InlineKeyboardButton(all_label, callback_data="cat_all"))
    if len(row) >= CATEGORY_BUTTONS_PER_ROW:
        buttons.append(row)
        row = []

    for name in CATEGORIES:
        label = f"✅ {name}" if current_category == name else name
        row.append(InlineKeyboardButton(label, callback_data=f"cat_{name}"))
        if len(row) >= CATEGORY_BUTTONS_PER_ROW:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def _categories_list_text() -> str:
    """Plain-text list of categories for non-premium users."""
    lines = ["🌐 All (default)"]
    for name in CATEGORIES:
        lines.append(f"• {name}")
    return "\n".join(lines)


def _invalidate_video_cache(category: str = None):
    """Invalidate one category's cache, or all caches."""
    if category:
        VIDEO_CACHE.pop(category, None)
    else:
        VIDEO_CACHE.clear()


async def _get_videos_for_category(category: str):
    """
    Return video list for a category, using per-category TTL cache.
    Does NOT fall back to 'all' if category is empty — returns [] so caller
    can show a proper "no files" message.
    """
    now = time.monotonic()
    cached = VIDEO_CACHE.get(category)
    if cached:
        videos, ts = cached
        if now - ts <= VIDEO_CACHE_TTL:
            return videos

    if category == "all":
        videos = await mdb.get_all_videos()
    else:
        channel_ids = CATEGORIES.get(category)
        if channel_ids:
            videos = await mdb.get_videos_by_channels(channel_ids)
        else:
            videos = await mdb.get_all_videos()

    VIDEO_CACHE[category] = (videos, now)
    return videos


# ==================== CACHE HELPERS ====================

async def get_cached_user_data(user_id: int):
    now = time.monotonic()
    if user_id in USER_DATA_CACHE:
        data, ts = USER_DATA_CACHE[user_id]
        if now - ts < USER_CACHE_TTL:
            return data
    user = await mdb.get_user(user_id)
    USER_DATA_CACHE[user_id] = (user, now)
    return user


async def get_cached_verification(user_id: int):
    now = time.monotonic()
    if user_id in VERIFICATION_CACHE:
        status, ts = VERIFICATION_CACHE[user_id]
        if now - ts < VERIFICATION_CACHE_TTL:
            return status
    verified, second, third = await asyncio.gather(
        udb.is_user_verified(user_id),
        udb.use_second_shortener(user_id, TWO_VERIFY_GAP),
        udb.use_third_shortener(user_id, THREE_VERIFY_GAP),
    )
    status = (verified, second, third)
    VERIFICATION_CACHE[user_id] = (status, now)
    return status


def clear_user_cache(user_id: int):
    USER_DATA_CACHE.pop(user_id, None)
    VERIFICATION_CACHE.pop(user_id, None)


async def get_bot_info(client):
    global BOT_INFO_CACHE
    if not BOT_INFO_CACHE:
        BOT_INFO_CACHE = await client.get_me()
    return BOT_INFO_CACHE


# Legacy compat
async def _get_videos():
    return await _get_videos_for_category("all")


# ==================== START COMMAND ====================

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    uid = message.from_user.id

    if await udb.is_user_banned(uid):
        await message.reply(
            "**🚫 You are banned from using this bot**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support", url=f"https://t.me/{ADMIN_USERNAME}")]])
        )
        return
    if IS_FSUB and not await get_fsub(client, message):
        return

    if len(message.command) > 1:
        data = message.command[1]
        if data.startswith("verify_"):
            await handle_verify(client, message, data)
            return
        elif data.startswith("link_"):
            link_id = data.split("_", 1)[1]
            from .link_generator import handle_link_access
            await handle_link_access(client, message, link_id)
            return
        elif data.startswith("share_"):
            link_id = data.split("_", 1)[1]
            from .callback import handle_share_link_access
            await handle_share_link_access(client, message, link_id)
            return

    user_check = udb.get_user(uid)
    if not await user_check:
        asyncio.create_task(register_user(client, message))

    await message.reply_photo(
        photo=random.choice(PICS),
        caption=text.START.format(message.from_user.mention),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")],
            [InlineKeyboardButton("🍿 𝖡𝗎𝗒 𝖲𝗎𝖻𝗌𝖼𝗋𝗂𝗉𝗍𝗂𝗈𝗇 🍾", callback_data="pro")],
            [InlineKeyboardButton("ℹ️ Disclaimer", callback_data="about"),
             InlineKeyboardButton("📚 Help", callback_data="help")]
        ])
    )


async def handle_verify(client, message, data):
    parts = data.split("_")
    if len(parts) < 4:
        return
    uid = int(parts[1])
    vid = parts[2]

    verify_info = await udb.get_verify_id_info(uid, vid)
    if not verify_info or verify_info.get("verified"):
        await message.reply("<b>Link expired</b>")
        return

    ist = pytz.timezone('Asia/Kolkata')
    is_second, is_third = await asyncio.gather(
        udb.use_second_shortener(uid, TWO_VERIFY_GAP),
        udb.user_verified(uid),
    )

    if is_third:
        key, num, msg = "third_time_verified", 3, text.THIRDT_VERIFY_COMPLETE_TEXT
    elif is_second:
        key, num, msg = "second_time_verified", 2, text.SECOND_VERIFY_COMPLETE_TEXT
    else:
        key, num, msg = "last_verified", 1, text.VERIFY_COMPLETE_TEXT

    now = datetime.now(tz=ist)
    await asyncio.gather(
        udb.update_verify_user(uid, {key: now}),
        udb.update_verify_id_info(uid, vid, {"verified": True})
    )
    clear_user_cache(uid)

    asyncio.create_task(client.send_message(
        LOG_VR_CHANNEL,
        text.VERIFIED_LOG_TEXT.format(message.from_user.mention, uid, now.strftime('%d %B %Y'), num)
    ))

    await message.reply_photo(
        photo=VERIFY_IMG,
        caption=msg.format(message.from_user.mention, get_readable_time(TWO_VERIFY_GAP)),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")]])
    )


async def register_user(client, message):
    await udb.addUser(message.from_user.id, message.from_user.first_name)
    bot_info = await get_bot_info(client)
    await client.send_message(LOG_CHNL, text.LOG.format(
        message.from_user.id,
        getattr(message.from_user, "dc_id", "N/A"),
        message.from_user.first_name or "N/A",
        f"@{message.from_user.username}" if message.from_user.username else "N/A",
        bot_info.username
    ))


# ==================== CATEGORY COMMAND ====================

@Client.on_message(filters.command("category") & filters.private)
async def category_command(client, message):
    uid = message.from_user.id

    if await udb.is_user_banned(uid):
        await message.reply("**🚫 You are banned from using this bot**")
        return
    if IS_FSUB and not await get_fsub(client, message):
        return

    user = await get_cached_user_data(uid)
    is_prime = user.get("plan") == "prime"

    if not CATEGORIES:
        await message.reply_text("📂 <b>No categories have been configured yet.</b>")
        return

    current_cat = await mdb.get_user_category(uid)

    if is_prime:
        markup = _build_category_markup(current_cat)
        markup.inline_keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close")])
        await message.reply_text(
            f"📂 <b>Choose a Category</b>\n\n"
            f"Current: <b>{'All' if current_cat == 'all' else current_cat}</b>\n\n"
            f"<i>Tap a category to switch:</i>",
            reply_markup=markup
        )
    else:
        cat_list = _categories_list_text()
        admin_id_int = int(ADMIN_ID) if isinstance(ADMIN_ID, int) else ADMIN_ID[0]
        await message.reply_text(
            f"🔒 <b>Categories — Premium Only</b>\n\n"
            f"<b>Available categories:</b>\n{cat_list}\n\n"
            f"<i>Upgrade to Premium to select a specific category!</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🍿 Buy Premium", callback_data="pro")],
                [InlineKeyboardButton("💳 Contact Admin", user_id=admin_id_int)],
            ])
        )


# ==================== VIDEO SENDING ====================

@Client.on_message(filters.command("getvideos") & filters.private)
async def get_video_cmd(client, message):
    await send_video(client, message)


def _make_file_buttons(uid: int, has_previous: bool) -> list:
    """Build the standard button rows for a file message."""
    buttons = []
    if has_previous:
        buttons.append([
            InlineKeyboardButton("⬅️ Back", callback_data=f"prev_{uid}"),
            InlineKeyboardButton("➡️ Next", callback_data="getvideo"),
        ])
    else:
        buttons.append([InlineKeyboardButton("➡️ Next", callback_data="getvideo")])
    buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{uid}")])
    buttons.append([InlineKeyboardButton("📂 Category", callback_data="show_category")])
    return buttons


async def _send_file(client, cid: int, file_id: str, media_type: str,
                     caption: str, protect: bool, buttons: list,
                     edit_message=None):
    """
    Send or edit a file of any media type.
    If edit_message is provided, always tries to edit it in-place using
    edit_message_media (works across ALL types — video→photo, photo→video etc).
    Falls back to delete+send only if edit fails.
    Returns the sent/edited message.
    """
    from pyrogram.types import (
        InputMediaVideo, InputMediaPhoto, InputMediaDocument,
        InputMediaAudio, InputMediaAnimation,
    )
    markup = InlineKeyboardMarkup(buttons)

    def _make_input_media(fid, mtype, cap):
        if mtype == "video":
            return InputMediaVideo(media=fid, caption=cap)
        elif mtype == "photo":
            return InputMediaPhoto(media=fid, caption=cap)
        elif mtype == "document":
            return InputMediaDocument(media=fid, caption=cap)
        elif mtype == "audio":
            return InputMediaAudio(media=fid, caption=cap)
        elif mtype == "animation":
            return InputMediaAnimation(media=fid, caption=cap)
        else:
            return InputMediaDocument(media=fid, caption=cap)

    if edit_message:
        try:
            edited = await edit_message.edit_media(
                _make_input_media(file_id, media_type, caption),
                reply_markup=markup,
            )
            # edit_media returns the updated message; fall back to edit_message if None
            return edited if edited is not None else edit_message
        except Exception:
            pass
        # edit failed → delete and resend fresh
        try:
            await edit_message.delete()
        except Exception:
            pass

    # Send fresh based on media type
    kwargs = dict(caption=caption, protect_content=protect, reply_markup=markup)
    if media_type == "video":
        return await client.send_video(cid, file_id, **kwargs)
    elif media_type == "photo":
        return await client.send_photo(cid, file_id, **kwargs)
    elif media_type == "document":
        return await client.send_document(cid, file_id, **kwargs)
    elif media_type == "audio":
        return await client.send_audio(cid, file_id, **kwargs)
    elif media_type == "voice":
        return await client.send_voice(cid, file_id, **kwargs)
    elif media_type == "animation":
        return await client.send_animation(cid, file_id, **kwargs)
    else:
        return await client.send_document(cid, file_id, **kwargs)


async def send_video(client, message, uid=None, delete_prev_msg=False):
    """
    Main file-sending function.
    delete_prev_msg=True: delete `message` before sending (used after category select).
    """
    uid = uid or message.from_user.id
    cid = message.chat.id

    banned, limits, user = await asyncio.gather(
        udb.is_user_banned(uid),
        mdb.get_global_limits(),
        get_cached_user_data(uid),
    )

    if banned:
        await message.reply("**🚫 You are banned from using this bot**")
        return

    if limits.get("maintenance"):
        await message.reply_text("**🛠️ Bot Under Maintenance — Back Soon!**")
        return

    if IS_FSUB and not await get_fsub(client, message, user_id=uid):
        return

    is_prime = user.get("plan") == "prime"

    if is_prime:
        usage_text = "🌟 User Plan : Prime"
    else:
        if IS_VERIFY:
            verified, is_second, is_third = await get_cached_verification(uid)
            if verified and not is_second and not is_third:
                usage_text = "**Status : ✅ Verified**"
            else:
                usage = await mdb.check_and_increment_usage(uid)
                if usage["allowed"]:
                    usage_text = f"📊 Daily Limit : {usage['count']}/{usage['limit']}"
                else:
                    await show_verify(client, message, uid, is_second, is_third)
                    return
        else:
            usage = await mdb.check_and_increment_usage(uid)
            if not usage["allowed"]:
                await message.reply_text(f"**🚫 Limit reached ({usage['limit']})\n\nUpgrade to Prime!**")
                return
            usage_text = f"📊 Daily Limit : {usage['count']}/{usage['limit']}"

    # ── get category-filtered videos ──────────────────────────────────────
    user_category = await mdb.get_user_category(uid) if is_prime else "all"
    videos = await _get_videos_for_category(user_category)

    if not videos:
        # Strictly no files in that category — do NOT fallback
        cat_name = "All" if user_category == "all" else user_category
        no_file_markup = None
        if is_prime and user_category != "all":
            no_file_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("📂 Change Category", callback_data="show_category")],
                [InlineKeyboardButton("🌐 Switch to All", callback_data="cat_all")],
            ])
        no_files_text = (
            f"📭 <b>No files found in category: {cat_name}</b>\n\n"
            f"<i>Try switching to a different category.</i>"
        )
        msg_has_media = any([
            getattr(message, "video", None),
            getattr(message, "photo", None),
            getattr(message, "document", None),
            getattr(message, "audio", None),
            getattr(message, "voice", None),
            getattr(message, "animation", None),
        ])
        if delete_prev_msg:
            try:
                await message.delete()
            except Exception:
                pass
            await client.send_message(cid, no_files_text, reply_markup=no_file_markup)
        elif msg_has_media:
            # Edit caption on the existing media message — no new message sent
            try:
                await message.edit_caption(no_files_text, reply_markup=no_file_markup)
            except Exception:
                await client.send_message(cid, no_files_text, reply_markup=no_file_markup)
        else:
            await message.reply_text(no_files_text, reply_markup=no_file_markup)
        return

    recent = USER_RECENT_VIDEOS.get(uid, set())
    available = [v for v in videos if v["video_id"] not in recent]
    if not available:
        USER_RECENT_VIDEOS[uid] = set()
        available = videos

    item = random.choice(available)
    USER_RECENT_VIDEOS.setdefault(uid, set()).add(item["video_id"])
    if len(USER_RECENT_VIDEOS[uid]) > 20:
        USER_RECENT_VIDEOS[uid] = set()

    file_id = item["file_id"]
    media_type = item.get("media_type", "video")
    mins = DELETE_TIMER // 60

    # Category line in caption (premium only)
    cat_display = ""
    if is_prime:
        cat_name = "All" if user_category == "all" else user_category
        cat_display = f"\n📂 Category: {cat_name}"

    caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}{cat_display}</b>"
    USER_CURRENT_VIDEO[uid] = file_id

    history = await mdb.get_watch_history(uid, limit=2)
    has_previous = len(history) > 0

    # protect_content — off for premium users if PREMIUM_CAN_DOWNLOAD is enabled
    protect = PROTECT_CONTENT
    if is_prime and PREMIUM_CAN_DOWNLOAD:
        protect = False

    buttons = _make_file_buttons(uid, has_previous)

    # Decide whether to edit existing message or delete+send fresh
    edit_msg = None
    if not delete_prev_msg:
        # Always try to edit the existing message in-place (works for all media types)
        # edit_message_media handles video→photo, photo→video transitions too
        msg_has_media = any([
            getattr(message, "video", None),
            getattr(message, "photo", None),
            getattr(message, "document", None),
            getattr(message, "audio", None),
            getattr(message, "voice", None),
            getattr(message, "animation", None),
        ])
        if msg_has_media:
            edit_msg = message

    if delete_prev_msg:
        try:
            await message.delete()
        except Exception:
            pass

    try:
        sent = await _send_file(
            client, cid, file_id, media_type, caption, protect, buttons,
            edit_message=edit_msg
        )
        asyncio.create_task(mdb.add_to_watch_history(uid, file_id, media_type))
        USER_ACTIVE_VIDEOS.setdefault(uid, set()).add(sent.id)
        asyncio.create_task(auto_delete(client, cid, sent.id, uid))
    except Exception as e:
        print(f"[send_video] error: {e}")
        await client.send_message(cid, "⚠️ Failed to send file.")


async def show_verify(client, message, uid, is_second, is_third):
    vid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
    TEMP_CHAT[uid] = message.chat.id
    bot_info = await get_bot_info(client)
    link = f"https://telegram.me/{bot_info.username}?start=verify_{uid}_{vid}_video"
    _, short = await asyncio.gather(
        udb.create_verify_id(uid, vid),
        get_shortlink(link, is_second, is_third),
    )
    if is_third:
        tut, msg = TUTORIAL3, text.THIRDT_VERIFICATION_TEXT
    elif is_second:
        tut, msg = TUTORIAL2, text.SECOND_VERIFICATION_TEXT
    else:
        tut, msg = TUTORIAL, text.VERIFICATION_TEXT
    sent = await message.reply_photo(
        photo=VERIFY_IMG,
        caption=msg.format(message.from_user.mention, "User", get_readable_time(TWO_VERIFY_GAP)),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Verify", url=short)],
            [InlineKeyboardButton("❓ How to verify", url=tut)]
        ])
    )
    asyncio.create_task(delete_later(sent, 300))


async def delete_later(msg, delay):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def auto_delete(client, cid, mid, uid):
    try:
        await asyncio.sleep(DELETE_TIMER)
        try:
            msg = await client.get_messages(cid, mid)
            if msg:
                fid = None
                if msg.video:        fid = msg.video.file_id
                elif msg.photo:      fid = msg.photo.file_id
                elif msg.document:   fid = msg.document.file_id
                elif msg.audio:      fid = msg.audio.file_id
                elif msg.animation:  fid = msg.animation.file_id
                if fid:
                    asyncio.create_task(mdb.clear_watch_history_for_file(fid))
        except Exception:
            pass
        try:
            await client.delete_messages(cid, mid)
        except Exception:
            pass
        if uid in USER_ACTIVE_VIDEOS:
            USER_ACTIVE_VIDEOS[uid].discard(mid)
            if not USER_ACTIVE_VIDEOS[uid]:
                USER_ACTIVE_VIDEOS.pop(uid, None)
                notif = await client.send_message(
                    cid,
                    "✅ File deleted due to inactivity.\n\nClick below to get a new file.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Get File", callback_data="getvideo")]])
                )
                asyncio.create_task(_delete_notif_after(client, cid, notif.id, 86400))
    except Exception as e:
        print(f"[auto_delete] error: {e}")


async def _delete_notif_after(client, cid, mid, delay):
    try:
        await asyncio.sleep(delay)
        await client.delete_messages(cid, mid)
    except Exception:
        pass
