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
INACTIVITY_TASKS = {}

async def get_updated_limits():
        global FREE_LIMIT, PRIME_LIMIT
        limits = await mdb.get_global_limits()
        FREE_LIMIT = limits["free_limit"]
        return limits

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
    task_key = f"{chat_id}_{user_id}"

    # Cancel previous timer
    if task_key in INACTIVITY_TASKS:
        INACTIVITY_TASKS[task_key].cancel()
        del INACTIVITY_TASKS[task_key]

    if await udb.is_user_banned(user_id):
        await message.reply("**🚫 You are banned from using this bot**")
        return

    limits = await get_updated_limits()

    if limits.get('maintenance', False):
        await message.reply_text("**🛠️ Bot Under Maintenance — Back Soon!**")
        return

    if IS_FSUB and not await get_fsub(client, message):
        return

    user = await mdb.get_user(user_id)
    plan = user.get("plan", "free")

    # FREE LIMIT CHECK
    if plan == "free":
        daily_count = user.get("daily_count", 0)
        if daily_count >= FREE_LIMIT:
            await message.reply_text(
                f"**🚫 You've reached your daily limit of {FREE_LIMIT} videos.\n\nUpgrade to Prime for unlimited access.**"
            )
            return

    # CACHE
    if "all" not in VIDEO_CACHE:
        VIDEO_CACHE["all"] = await mdb.get_all_videos()

    videos = VIDEO_CACHE["all"]

    if not videos:
        await message.reply_text("No videos available.")
        return

    random_video = random.choice(videos)
    channel_msg_id = random_video["video_id"]

    # Fetch original message to get file_id
    original_msg = await client.get_messages(DATABASE_CHANNEL_ID, channel_msg_id)

    if not original_msg.video:
        await message.reply_text("Invalid video data.")
        return

    file_id = original_msg.video.file_id

    delete_minutes = DELETE_TIMER // 60

    caption_text = (
        f"<b><blockquote>"
        f"⚠️ This video will auto delete in {delete_minutes} minutes.\n\n"
        f"💾 Save it if needed!"
        f"</blockquote></b>"
    )

    try:

        if message.video:
            await message.edit_media(
                InputMediaVideo(
                    media=file_id,
                    caption=caption_text
                ),
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

        if plan == "free":
            await mdb.increment_daily_count(user_id)

        task = asyncio.create_task(
            inactivity_delete(client, chat_id, sent_message.id, user_id)
        )
        INACTIVITY_TASKS[task_key] = task

    except Exception as e:
        print(f"Edit error: {e}")
        await message.reply_text("Failed to load video.")

async def inactivity_delete(client: Client, chat_id: int, message_id: int, user_id: int):
    try:
        await asyncio.sleep(DELETE_TIMER)

        task_key = f"{chat_id}_{user_id}"

        if task_key in INACTIVITY_TASKS:
            await client.delete_messages(chat_id, message_id)

            await client.send_message(
                chat_id,
                "✅ Video deleted successfully.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")]]
                )
            )

            del INACTIVITY_TASKS[task_key]

    except Exception as e:
        print(f"Inactivity delete error: {e}")





