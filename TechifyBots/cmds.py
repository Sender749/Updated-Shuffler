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

VIDEO_CACHE = {}
USER_ACTIVE_VIDEOS = {}
USER_RECENT_VIDEOS = {}
TEMP_CHAT = {}

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    if await udb.is_user_banned(message.from_user.id):
        await message.reply("**🚫 You are banned from using this bot**",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support 🧑‍💻", url=f"https://t.me/{ADMIN_USERNAME}")]]))
        return
    
    # Handle verification callback
    if len(message.command) > 1:
        data = message.command[1]
        if data.startswith("verify_"):
            parts = data.split("_")
            if len(parts) >= 4:
                _, user_id, verify_id, video_id = parts[0], int(parts[1]), parts[2], parts[3]
                
                # Verify the token
                verify_info = await udb.get_verify_id_info(user_id, verify_id)
                
                if not verify_info or verify_info.get("verified"):
                    await message.reply("<b>ʟɪɴᴋ ᴇxᴘɪʀᴇᴅ ᴛʀʏ ᴀɢᴀɪɴ...</b>")
                    return
                
                ist_timezone = pytz.timezone('Asia/Kolkata')
                
                # Determine which verification stage
                is_second = await udb.use_second_shortener(user_id, TWO_VERIFY_GAP)
                is_third = await udb.user_verified(user_id)
                
                if is_third:
                    key = "third_time_verified"
                    verify_num = 3
                    msg = text.THIRDT_VERIFY_COMPLETE_TEXT
                elif is_second:
                    key = "second_time_verified"
                    verify_num = 2
                    msg = text.SECOND_VERIFY_COMPLETE_TEXT
                else:
                    key = "last_verified"
                    verify_num = 1
                    msg = text.VERIFY_COMPLETE_TEXT
                
                current_time = datetime.now(tz=ist_timezone)
                
                # Update verification time
                await udb.update_verify_user(user_id, {key: current_time})
                await udb.update_verify_id_info(user_id, verify_id, {"verified": True})
                
                # Log verification
                await client.send_message(
                    LOG_VR_CHANNEL,
                    text.VERIFIED_LOG_TEXT.format(
                        message.from_user.mention,
                        user_id,
                        current_time.strftime('%d %B %Y'),
                        verify_num
                    )
                )
                
                # Send success message
                await message.reply_photo(
                    photo=VERIFY_IMG,
                    caption=msg.format(message.from_user.mention, get_readable_time(TWO_VERIFY_GAP)),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🎬 Get Video Now", callback_data="getvideo")
                    ]])
                )
                return
    
    if IS_FSUB and not await get_fsub(client, message):return
    if await udb.get_user(message.from_user.id) is None:
        await udb.addUser(message.from_user.id, message.from_user.first_name)
        bot_obj = await client.get_me()
        await client.send_message(
            LOG_CHNL,
            text.LOG.format(
                message.from_user.id,
                getattr(message.from_user, "dc_id", "N/A"),
                message.from_user.first_name or "N/A",
                f"@{message.from_user.username}" if message.from_user.username else "N/A",
                bot_obj.username
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

async def send_video_logic(client: Client, message: Message, user_id: int = None):
    if user_id is None:
        user_id = message.from_user.id
    chat_id = message.chat.id

    if await udb.is_user_banned(user_id):
        await message.reply("**🚫 You are banned from using this bot**")
        return

    limits = await mdb.get_global_limits()
    if limits.get("maintenance", False):
        await message.reply_text("**🛠️ Bot Under Maintenance — Back Soon!**")
        return

    if IS_FSUB and not await get_fsub(client, message):
        return

    # Get user plan
    user = await mdb.get_user(user_id)
    is_prime = user.get("plan", "free") == "prime"
    
    # If user is prime, skip verification and limit checks
    if is_prime:
        usage = await mdb.check_and_increment_usage(user_id)
        if not usage["allowed"]:
            await message.reply_text(
                f"**🚫 You've reached your daily limit of {usage['limit']} videos.\n\nUpgrade to Prime for unlimited access.**"
            )
            return
        usage_text = "🌟 Prime User: Unlimited Access"
    else:
        # For free users, check verification first
        if IS_VERIFY:
            user_verified = await udb.is_user_verified(user_id)
            is_second_shortener = await udb.use_second_shortener(user_id, TWO_VERIFY_GAP)
            is_third_shortener = await udb.use_third_shortener(user_id, THREE_VERIFY_GAP)
            
            # If not verified or verification expired, show verification message
            if not user_verified or is_second_shortener or is_third_shortener:
                # Create verification ID
                verify_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
                await udb.create_verify_id(user_id, verify_id)
                
                # Store user_id for later use
                TEMP_CHAT[user_id] = chat_id
                
                # Get appropriate shortlink
                bot_info = await client.get_me()
                verify_link = f"https://telegram.me/{bot_info.username}?start=verify_{user_id}_{verify_id}_video"
                short_link = await get_shortlink(verify_link, is_second_shortener, is_third_shortener)
                
                # Select appropriate tutorial based on verification stage
                if is_third_shortener:
                    tutorial_link = TUTORIAL3
                    msg_text = text.THIRDT_VERIFICATION_TEXT
                elif is_second_shortener:
                    tutorial_link = TUTORIAL2
                    msg_text = text.SECOND_VERIFICATION_TEXT
                else:
                    tutorial_link = TUTORIAL
                    msg_text = text.VERIFICATION_TEXT
                
                buttons = [
                    [InlineKeyboardButton(text="♻️ ᴠᴇʀɪғʏ ♻️", url=short_link)],
                    [InlineKeyboardButton(text="❗️ ʜᴏᴡ ᴛᴏ ᴠᴇʀɪғʏ ❓", url=tutorial_link)]
                ]
                
                # Send verification message
                sent = await message.reply_photo(
                    photo=VERIFY_IMG,
                    caption=msg_text.format(message.from_user.mention, "User", get_readable_time(TWO_VERIFY_GAP)),
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                
                # Auto-delete verification message after 5 minutes
                await asyncio.sleep(300)
                try:
                    await sent.delete()
                except:
                    pass
                return
        
        # Check usage limit for free users who are verified
        usage = await mdb.check_and_increment_usage(user_id)
        
        if not usage["allowed"]:
            await message.reply_text(
                f"**🚫 You've reached your daily limit of {usage['limit']} videos.\n\nUpgrade to Prime for unlimited access or verify to get unlimited videos for today.**"
            )
            return
        
        usage_text = f"📊 Limit: {usage['count']}/{usage['limit']}"

    # Load videos
    if "all" not in VIDEO_CACHE:
        VIDEO_CACHE["all"] = await mdb.get_all_videos()

    videos = VIDEO_CACHE["all"]

    if not videos:
        await message.reply_text("No videos available.")
        return

    # Prevent repeats
    recent = USER_RECENT_VIDEOS.get(user_id, set())
    available_videos = [v for v in videos if v["video_id"] not in recent]

    if not available_videos:
        USER_RECENT_VIDEOS[user_id] = set()
        available_videos = videos

    random_video = random.choice(available_videos)

    USER_RECENT_VIDEOS.setdefault(user_id, set()).add(random_video["video_id"])
    if len(USER_RECENT_VIDEOS[user_id]) > 10:
        USER_RECENT_VIDEOS[user_id].pop()

    file_id = random_video.get("file_id")
    channel_msg_id = random_video["video_id"]
    delete_minutes = DELETE_TIMER // 60

    caption_text = (
        f"<b><blockquote>"
        f"⚠️ This video will auto delete in {delete_minutes} minutes."
        f"</blockquote>\n\n"
        f"🆔 File ID: <code>{channel_msg_id}</code>\n"
        f"{usage_text}"
        f"</b>"
    )

    try:
        if message.video:
            await message.edit_media(
                InputMediaVideo(media=file_id, caption=caption_text),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎬 Next Video", callback_data="getvideo")]]
                )
            )
            sent_message = message
        else:
            sent_message = await client.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption_text,
                protect_content=PROTECT_CONTENT,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎬 Next Video", callback_data="getvideo")]]
                )
            )

        USER_ACTIVE_VIDEOS.setdefault(user_id, set()).add(sent_message.id)
        asyncio.create_task(auto_delete_video(client, chat_id, sent_message.id, user_id))

    except Exception as e:
        print(f"Video send error: {e}")
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








