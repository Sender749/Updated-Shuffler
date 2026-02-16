from pyrogram import Client, filters
from pyrogram.types import *
from vars import *
from Database.maindb import mdb
from Database.userdb import udb
from datetime import datetime
import pytz, random, asyncio, string
from .fsub import get_fsub
from Script import text
from .utils import get_shortlink, get_readable_time
from bot import bot

# ==================== PERFORMANCE CACHES ====================
VIDEO_CACHE = {}
USER_ACTIVE_VIDEOS = {}
USER_RECENT_VIDEOS = {}
TEMP_CHAT = {}

# User data cache - 60 second TTL
USER_DATA_CACHE = {}
USER_CACHE_TTL = 60

# Bot info cache - permanent
BOT_INFO_CACHE = None

# Verification cache - 30 second TTL
VERIFICATION_CACHE = {}
VERIFICATION_CACHE_TTL = 30

# ==================== CACHE HELPERS ====================

async def get_cached_user_data(user_id: int):
    """Get user with 60s cache"""
    now = datetime.now().timestamp()
    if user_id in USER_DATA_CACHE:
        data, ts = USER_DATA_CACHE[user_id]
        if now - ts < USER_CACHE_TTL:
            return data
    user = await mdb.get_user(user_id)
    USER_DATA_CACHE[user_id] = (user, now)
    return user

async def get_cached_verification(user_id: int):
    """Get verification with 30s cache"""
    now = datetime.now().timestamp()
    if user_id in VERIFICATION_CACHE:
        status, ts = VERIFICATION_CACHE[user_id]
        if now - ts < VERIFICATION_CACHE_TTL:
            return status
    verified = await udb.is_user_verified(user_id)
    second = await udb.use_second_shortener(user_id, TWO_VERIFY_GAP)
    third = await udb.use_third_shortener(user_id, THREE_VERIFY_GAP)
    status = (verified, second, third)
    VERIFICATION_CACHE[user_id] = (status, now)
    return status

def clear_user_cache(user_id: int):
    """Clear cache after updates"""
    USER_DATA_CACHE.pop(user_id, None)
    VERIFICATION_CACHE.pop(user_id, None)

async def get_bot_info(client):
    """Get bot info (cached permanently)"""
    global BOT_INFO_CACHE
    if not BOT_INFO_CACHE:
        BOT_INFO_CACHE = await client.get_me()
    return BOT_INFO_CACHE

# ==================== START COMMAND ====================

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    uid = message.from_user.id
    
    if await udb.is_user_banned(uid):
        await message.reply("**🚫 Banned**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support", url=f"https://t.me/{ADMIN_USERNAME}")]]))
        return
    
    # Handle verification callback
    if len(message.command) > 1:
        data = message.command[1]
        if data.startswith("verify_"):
            await handle_verify(client, message, data)
            return
        elif data.startswith("link_"):
            # Handle multi-file link access
            link_id = data.split("_", 1)[1]
            from .link_generator import handle_link_access
            await handle_link_access(client, message, link_id)
            return
        elif data.startswith("share_"):
            # Handle single file share link access
            link_id = data.split("_", 1)[1]
            from .callback import handle_share_link_access
            await handle_share_link_access(client, message, link_id)
            return
    
    # Parallel checks
    fsub = get_fsub(client, message) if IS_FSUB else None
    user_check = udb.get_user(uid)
    
    if IS_FSUB and not await fsub:
        return
    
    # Register new user async
    if not await user_check:
        asyncio.create_task(register_user(client, message))
    
    # Instant welcome
    await message.reply_photo(
        photo=random.choice(PICS),
        caption=text.START.format(message.from_user.mention),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")],
            [InlineKeyboardButton("🍿 Premium", callback_data="pro")],
            [InlineKeyboardButton("ℹ️ About", callback_data="about"), InlineKeyboardButton("📚 Help", callback_data="help")]
        ])
    )

async def handle_verify(client, message, data):
    """Handle verification"""
    parts = data.split("_")
    if len(parts) < 4:
        return
    _, uid, vid, _ = parts[0], int(parts[1]), parts[2], parts[3]
    
    verify_info = await udb.get_verify_id_info(uid, vid)
    if not verify_info or verify_info.get("verified"):
        await message.reply("<b>Link expired</b>")
        return
    
    ist = pytz.timezone('Asia/Kolkata')
    is_second = await udb.use_second_shortener(uid, TWO_VERIFY_GAP)
    is_third = await udb.user_verified(uid)
    
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
    
    asyncio.create_task(client.send_message(LOG_VR_CHANNEL, text.VERIFIED_LOG_TEXT.format(message.from_user.mention, uid, now.strftime('%d %B %Y'), num)))
    
    await message.reply_photo(
        photo=VERIFY_IMG,
        caption=msg.format(message.from_user.mention, get_readable_time(TWO_VERIFY_GAP)),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")]])
    )

async def register_user(client, message):
    """Register user async"""
    await udb.addUser(message.from_user.id, message.from_user.first_name)
    bot_info = await get_bot_info(client)
    await client.send_message(LOG_CHNL, text.LOG.format(
        message.from_user.id,
        getattr(message.from_user, "dc_id", "N/A"),
        message.from_user.first_name or "N/A",
        f"@{message.from_user.username}" if message.from_user.username else "N/A",
        bot_info.username
    ))

# ==================== VIDEO SENDING ====================

@Client.on_message(filters.command("getvideos") & filters.private)
async def get_video_cmd(client, message):
    await send_video(client, message)

async def send_video(client, message, uid=None):
    """Optimized video sending"""
    uid = uid or message.from_user.id
    cid = message.chat.id
    
    # Parallel checks
    banned, limits, user = await asyncio.gather(
        udb.is_user_banned(uid),
        mdb.get_global_limits(),
        get_cached_user_data(uid)
    )
    
    if banned:
        await message.reply("**🚫 Banned**")
        return
    
    if limits.get("maintenance"):
        await message.reply_text("**🛠️ Maintenance**")
        return
    
    if IS_FSUB and not await get_fsub(client, message):
        return
    
    # Check user status
    is_prime = user.get("plan") == "prime"
    
    if is_prime:
        usage_text = "🌟 Prime"
    else:
        if IS_VERIFY:
            verified, is_second, is_third = await get_cached_verification(uid)
            
            if verified and not is_second and not is_third:
                usage_text = "✅ Verified"
            else:
                usage = await mdb.check_and_increment_usage(uid)
                if usage["allowed"]:
                    usage_text = f"📊 {usage['count']}/{usage['limit']}"
                else:
                    await show_verify(client, message, uid, is_second, is_third)
                    return
        else:
            usage = await mdb.check_and_increment_usage(uid)
            if not usage["allowed"]:
                await message.reply_text(f"**🚫 Limit reached ({usage['limit']})\n\nUpgrade to Prime!**")
                return
            usage_text = f"📊 {usage['count']}/{usage['limit']}"
    
    # Get video
    if "all" not in VIDEO_CACHE:
        VIDEO_CACHE["all"] = await mdb.get_all_videos()
    
    videos = VIDEO_CACHE["all"]
    if not videos:
        await message.reply_text("No videos")
        return
    
    recent = USER_RECENT_VIDEOS.get(uid, set())
    available = [v for v in videos if v["video_id"] not in recent] or videos
    
    if not available:
        USER_RECENT_VIDEOS[uid] = set()
        available = videos
    
    video = random.choice(available)
    USER_RECENT_VIDEOS.setdefault(uid, set()).add(video["video_id"])
    if len(USER_RECENT_VIDEOS[uid]) > 10:
        USER_RECENT_VIDEOS[uid].pop()
    
    # Send video
    file_id = video["file_id"]
    mins = DELETE_TIMER // 60
    caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}</b>"
    
    # Check if user has watch history for back button
    history = await mdb.get_watch_history(uid, limit=2)
    has_previous = len(history) > 0
    
    # Build buttons
    buttons = []
    if has_previous:
        buttons.append([
            InlineKeyboardButton("⬅️ Back", callback_data=f"previous_{file_id}"),
            InlineKeyboardButton("➡️ Next", callback_data="getvideo")
        ])
    else:
        buttons.append([InlineKeyboardButton("➡️ Next", callback_data="getvideo")])
    
    buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{file_id}")])
    
    try:
        if message.video:
            await message.edit_media(
                InputMediaVideo(media=file_id, caption=caption), 
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            sent = message
        else:
            sent = await client.send_video(
                cid, 
                file_id, 
                caption=caption, 
                protect_content=PROTECT_CONTENT, 
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        
        # Add to watch history
        await mdb.add_to_watch_history(uid, file_id, "video")
        
        USER_ACTIVE_VIDEOS.setdefault(uid, set()).add(sent.id)
        asyncio.create_task(auto_delete(client, cid, sent.id, uid))
    except Exception as e:
        print(f"Video error: {e}")
        await message.reply_text("⚠️ Failed")

async def show_verify(client, message, uid, is_second, is_third):
    """Show verification"""
    vid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
    TEMP_CHAT[uid] = message.chat.id
    
    bot_info = await get_bot_info(client)
    link = f"https://telegram.me/{bot_info.username}?start=verify_{uid}_{vid}_video"
    
    db_task = udb.create_verify_id(uid, vid)
    short_task = get_shortlink(link, is_second, is_third)
    _, short = await asyncio.gather(db_task, short_task)
    
    if is_third:
        tut, msg = TUTORIAL3, text.THIRDT_VERIFICATION_TEXT
    elif is_second:
        tut, msg = TUTORIAL2, text.SECOND_VERIFICATION_TEXT
    else:
        tut, msg = TUTORIAL, text.VERIFICATION_TEXT
    
    sent = await message.reply_photo(
        photo=VERIFY_IMG,
        caption=msg.format(message.from_user.mention, "User", get_readable_time(TWO_VERIFY_GAP)),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Verify", url=short)], [InlineKeyboardButton("❓ How to verify", url=tut)]])
    )
    asyncio.create_task(delete_later(sent, 300))

async def delete_later(msg, delay):
    """Delete after delay"""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

async def auto_delete(client, cid, mid, uid):
    """Auto-delete video"""
    try:
        await asyncio.sleep(DELETE_TIMER)
        
        # Get the file_id before deleting
        try:
            msg = await client.get_messages(cid, mid)
            if msg.video:
                file_id = msg.video.file_id
                # Clear this file from all users' watch history
                await mdb.clear_watch_history_for_file(file_id)
        except:
            pass
        
        try:
            await client.delete_messages(cid, mid)
        except:
            pass
        if uid in USER_ACTIVE_VIDEOS:
            USER_ACTIVE_VIDEOS[uid].discard(mid)
            if not USER_ACTIVE_VIDEOS[uid]:
                USER_ACTIVE_VIDEOS.pop(uid, None)
                await client.send_message(cid, "✅ Video Deletd, due to inactivity.\n\nClick below button to get new video.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 More", callback_data="getvideo")]]))
    except Exception as e:
        print(f"Delete error: {e}")

