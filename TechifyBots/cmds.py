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
USER_RECENT_VIDEOS = {}

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

    # ✅ Centralized plan + limit check
    usage = await mdb.check_and_increment_usage(user_id)

    if not usage["allowed"]:
        await message.reply_text(
            f"**🚫 You've reached your daily limit of {usage['limit']} videos.\n\nUpgrade to Prime for unlimited access.**"
        )
        return

    # Prepare usage text
    if usage["plan"] == "prime":
        usage_text = "🌟 Prime User: Unlimited Access"
    else:
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







