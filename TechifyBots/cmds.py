from pyrogram import Client, filters
from pyrogram.types import *
from vars import *
from Database.maindb import mdb
from Database.userdb import udb
from datetime import datetime
import pytz, random, asyncio
from .fsub import get_fsub
from Script import text

VIDEO_CACHE = {}
USER_ACTIVE_VIDEOS = {}

async def check_and_get_user_status(user_id: int):
    """Check user status and return (is_prime, usage_text, can_continue)"""
    user = await mdb.get_user(user_id)
    plan = user.get("plan", "free")
    
    # Check if premium has expired
    if plan == "prime":
        prime_expiry = user.get("prime_expiry")
        if prime_expiry and prime_expiry < datetime.now():
            await mdb.remove_premium(user_id)
            user = await mdb.get_user(user_id)
            plan = "free"
    
    if plan == "prime":
        return True, "🌟 Prime User: Unlimited Access", True
    
    # Free user - check limit
    limits = await mdb.get_global_limits()
    FREE_LIMIT = limits["free_limit"]
    daily_count = user.get("daily_count", 0)
    
    if daily_count >= FREE_LIMIT:
        return False, f"📊 Limit: {daily_count}/{FREE_LIMIT}", False
    
    return False, f"📊 Limit: {daily_count}/{FREE_LIMIT}", True

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    if await udb.is_user_banned(message.from_user.id):
        await message.reply("**🚫 You are banned from using this bot**",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support 🧑‍💻", url=f"https://t.me/{ADMIN_USERNAME}")]]))
        return
    if IS_FSUB and not await get_fsub(client, message):return
    if await udb.get_user(message.from_user.id) is None:
        await udb.addUser(message.from_user.id, message.from_user.first_name)
        bot = await client.get_me()
        await client.send_message(
            LOG_CHNL,
            text.LOG.format(
                message.from_user.id,
                getattr(message.from_user, "dc_id", "N/A"),
                message.from_user.first_name or "N/A",
                f"@{message.from_user.username}" if message.from_user.username else "N/A",
                bot.username
            )
        )
    await message.reply_photo(
        photo=random.choice(PICS),
        caption=text.START.format(message.from_user.mention),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")],
            [InlineKeyboardButton("🍿 𝖡𝗎𝗒 𝖲𝗎𝖻𝗌𝖼𝗋𝗂𝗉𝗍𝗂𝗈𝗇 🍾", callback_data="pro")],
            [InlineKeyboardButton("ℹ️ 𝖠𝖻𝗈𝗎𝗍", callback_data="about"),
             InlineKeyboardButton("📚 𝖧𝖾𝗅𝗉", callback_data="help")] 
        ])
    )

@Client.on_message(filters.command("getvideos") & filters.private)
async def send_random_video(client: Client, message: Message):
    await send_video_logic(client, message)

async def send_video_logic(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    if await udb.is_user_banned(user_id):
        await message.reply("**🚫 You are banned from using this bot**")
        return
    
    limits = await mdb.get_global_limits()
    if limits.get('maintenance', False):
        await message.reply_text("**🛠️ Bot Under Maintenance — Back Soon!**")
        return
    
    if IS_FSUB and not await get_fsub(client, message):
        return
    
    # Check user status
    is_prime, usage_text, can_continue = await check_and_get_user_status(user_id)
    
    if not can_continue:
        FREE_LIMIT = limits["free_limit"]
        await message.reply_text(f"**🚫 You've reached your daily limit of {FREE_LIMIT} videos.\n\nUpgrade to Prime for unlimited access.**")
        return
    
    # Increment count only for free users
    if not is_prime:
        await mdb.increment_daily_count(user_id)
        # Refresh usage text after increment
        _, usage_text, _ = await check_and_get_user_status(user_id)
    
    if "all" not in VIDEO_CACHE:
        VIDEO_CACHE["all"] = await mdb.get_all_videos()
    
    videos = VIDEO_CACHE["all"]
    if not videos:
        await message.reply_text("No videos available.")
        return
    
    random_video = random.choice(videos)
    video_id = random_video["video_id"]
    channel_id = random_video.get("channel_id", DATABASE_CHANNEL_ID[0])
    
    try:
        original_msg = await client.get_messages(channel_id, video_id)
    except Exception as e:
        print(f"Error fetching video: {e}")
        await message.reply_text("Failed to fetch video. It may have been deleted.")
        return
    
    if not original_msg or not original_msg.video:
        await message.reply_text("Invalid video data.")
        return
    
    file_id = original_msg.video.file_id
    delete_minutes = DELETE_TIMER // 60
    caption_text = (f"<b><blockquote>⚠️ This video will auto delete in {delete_minutes} minutes.</blockquote>\n\n🆔 File ID: <code>{video_id}</code>\n{usage_text}</b>")
    
    try:
        if message.video:
            await message.edit_media(
                InputMediaVideo(media=file_id, caption=caption_text),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Next Video", callback_data="getvideo")]]))
            sent_message = message
        else:
            sent_message = await client.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption_text,
                protect_content=PROTECT_CONTENT,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Next Video", callback_data="getvideo")]]))
        USER_ACTIVE_VIDEOS.setdefault(user_id, set()).add(sent_message.id)
        asyncio.create_task(auto_delete_video(client, chat_id, sent_message.id, user_id))
    except Exception as e:
        print(f"Send video error: {e}")
        await message.reply_text("Failed to load video.")

async def auto_delete_video(client: Client, chat_id: int, message_id: int, user_id: int):
    try:
        await asyncio.sleep(DELETE_TIMER)
        try:
            await client.delete_messages(chat_id, message_id)
        except:
            pass 
        if user_id in USER_ACTIVE_VIDEOS:
            USER_ACTIVE_VIDEOS[user_id].discard(message_id)
            if not USER_ACTIVE_VIDEOS[user_id]:
                USER_ACTIVE_VIDEOS.pop(user_id, None)
                await client.send_message(chat_id, "✅ Video deleted successfully.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")]]))
    except Exception as e:
        print(f"Auto delete error: {e}")
