from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from pyrogram import Client
from Script import text
from vars import ADMIN_ID, DELETE_TIMER, PROTECT_CONTENT
from Database.maindb import mdb
from .cmds import send_video, get_cached_user_data, USER_ACTIVE_VIDEOS, USER_CURRENT_VIDEO
from .index import INDEX_TASKS, start_indexing
import asyncio, string, random
from datetime import datetime

@Client.on_callback_query()
async def callback_query_handler(client, query: CallbackQuery):
    try:
        if query.data == "start":
            try:
                await query.message.edit_caption(
                    caption=text.START.format(query.from_user.mention),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")],
                        [InlineKeyboardButton("🍿 𝖡𝗎𝗒 𝖲𝗎𝖻𝗌𝖼𝗋𝗂𝗉𝗍𝗂𝗈𝗇 🍾", callback_data="pro")],
                        [InlineKeyboardButton("ℹ️ Disclaimer", callback_data="about"), InlineKeyboardButton("📚 𝖧𝖾𝗅𝗉", callback_data="help")]])
                )
            except:
                pass
        elif query.data.startswith("index_select_"):
            await query.answer()
            channel_id = int(query.data.split("_")[-1])
            try:
                await query.message.edit_text(
                    f"**Send Skip Message ID or Message Link**\n\nChannel: `{channel_id}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]))
            except:
                pass
            INDEX_TASKS[query.from_user.id] = {"channel_id": channel_id,"state": "await_skip", "msg_id": query.message.id}
            return
        
        elif query.data == "index_cancel":
            await query.answer()
            user_id = query.from_user.id
            task = INDEX_TASKS.get(user_id)
            if task:
                task["cancel"] = True
            try:
                await query.message.edit_text("❌ Indexing Cancelled.")
            except:
                pass
            return

        elif query.data == "help":
            await query.message.edit_caption(
                caption=text.HELP,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 𝖠𝖽𝗆𝗂𝗇 𝖢𝗈𝗆𝗆𝖺𝗇𝖽𝗌", callback_data="admincmds")],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")]
                ])
            )

        elif query.data == "about":
            await query.message.edit_caption(
                caption=text.ABOUT,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍💻 𝖣𝖾𝗏𝖾𝗅𝗈𝗉𝖾𝗋 👨‍💻", user_id=int(ADMIN_ID))],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")]
                ])
            )

        elif query.data == "pro":
            current_limits = await mdb.get_global_limits()
            pro_text = text.PRO.format(free_limit=current_limits['free_limit'])
            await query.message.edit_caption(
                caption=pro_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Send Screenshot", user_id=int(ADMIN_ID))],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")]
                ])
            )

        elif query.data == "admincmds":
            if query.from_user.id != ADMIN_ID:
                await query.answer("You are not my admin ❌", show_alert=True)
            else:
                await query.message.edit_caption(
                    caption=text.ADMIN_COMMANDS,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="help")]
                    ])
                )

        elif query.data == "getvideo":
            await query.answer()
            await send_video(client, query.message, uid=query.from_user.id)
        
        elif query.data.startswith("prev_"):
            await query.answer()
            await handle_previous_video(client, query)
        
        elif query.data.startswith("share_"):
            await query.answer()
            await handle_share_video(client, query)
 

        elif query.data == "close":
            await query.message.delete()

    except Exception as e:
        print(f"Callback error: {e}")
        await query.answer("⚠️ An error occurred. Try again later.", show_alert=True)

# ==================== PREVIOUS VIDEO HANDLER ====================

async def handle_previous_video(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    current_file_id = USER_CURRENT_VIDEO.get(user_id)
    if not current_file_id:
        await query.answer("❌ No current video found", show_alert=True)
        return
    prev_video = await mdb.get_previous_video(user_id, current_file_id)
    if not prev_video:
        await query.answer("❌ No previous video in history", show_alert=True)
        return
    user = await get_cached_user_data(user_id)
    is_prime = user.get("plan") == "prime"
    if is_prime:
        usage_text = "🌟 User Plan : Prime"
    else:
        # Don't increment usage for going back
        from Database.userdb import udb
        user_data = await mdb.get_user(user_id)
        daily_count = user_data.get("daily_count", 0)
        limits = await mdb.get_global_limits()
        usage_text = f"📊 Daily Limit : {daily_count}/{limits['free_limit']}"
    
    mins = DELETE_TIMER // 60
    caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}</b>"
    history = await mdb.get_watch_history(user_id, limit=50)
    prev_file_id = prev_video["file_id"]
    USER_CURRENT_VIDEO[user_id] = prev_file_id
    current_index = None
    for idx, item in enumerate(history):
        if item["file_id"] == prev_file_id:
            current_index = idx
            break
    has_previous = current_index is not None and current_index + 1 < len(history)
    buttons = []
    if has_previous:
        buttons.append([
            InlineKeyboardButton("⬅️ Back", callback_data=f"prev_{user_id}"),
            InlineKeyboardButton("➡️ Next", callback_data="getvideo")
        ])
    else:
        buttons.append([InlineKeyboardButton("➡️ Next", callback_data="getvideo")])
    buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{user_id}")])
    try:
        media_type = prev_video.get("media_type", "video")
        if media_type == "video":
            await query.message.edit_media(
                InputMediaVideo(media=prev_file_id, caption=caption),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await query.message.delete()
            if media_type == "photo":
                await client.send_photo(
                    query.message.chat.id,
                    prev_file_id,
                    caption=caption,
                    protect_content=PROTECT_CONTENT,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            elif media_type == "document":
                await client.send_document(
                    query.message.chat.id,
                    prev_file_id,
                    caption=caption,
                    protect_content=PROTECT_CONTENT,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
    except Exception as e:
        print(f"Previous video error: {e}")
        await query.answer("⚠️ Failed to load previous video", show_alert=True)

# ==================== SHARE VIDEO HANDLER ====================

async def handle_share_video(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    file_id = USER_CURRENT_VIDEO.get(user_id)
    if not file_id:
        await query.answer("❌ No current video found", show_alert=True)
        return
    link_id = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    media_type = "video"  
    if query.message.photo:
        media_type = "photo"
    elif query.message.document:
        media_type = "document"
    elif query.message.video:
        media_type = "video"
    await mdb.async_db["share_links"].insert_one({
        "link_id": link_id,
        "file_id": file_id,
        "media_type": media_type,
        "shared_by": user_id,
        "created_at": datetime.now(),
        "access_count": 0
    })
    bot_info = await client.get_me()
    link = f"https://t.me/{bot_info.username}?start=share_{link_id}"
    await query.message.reply_text(
        f"🔗 **Share Link Generated!**\n\n"
        f"`{link}`\n\n"
        f"Share with your buddies 😉",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Copy Link", url=link)]]))
    await query.answer("✅ Share link generated!", show_alert=False)

# ==================== HANDLE SHARE LINK ACCESS ====================
async def handle_share_link_access(client: Client, message, link_id: str):
    link_data = await mdb.async_db["share_links"].find_one({"link_id": link_id})
    if not link_data:
        await message.reply_text("❌ Invalid or expired share link.")
        return
    file_id = link_data["file_id"]
    media_type = link_data["media_type"]
    await mdb.async_db["share_links"].update_one(
        {"link_id": link_id},
        {"$inc": {"access_count": 1}}
    )
    try:
        if media_type == "video":
            await client.send_video(
                message.chat.id,
                file_id,
                caption="🔗 **Click below button to get more videos❗**",
                protect_content=PROTECT_CONTENT,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")
                ]])
            )
        elif media_type == "photo":
            await client.send_photo(
                message.chat.id,
                file_id,
                caption="🔗 **Click below button to get more videos❗**",
                protect_content=PROTECT_CONTENT,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")
                ]])
            )
        elif media_type == "document":
            await client.send_document(
                message.chat.id,
                file_id,
                caption="🔗 **Click below button to get more videos❗**",
                protect_content=PROTECT_CONTENT,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")
                ]])
            )
    except Exception as e:
        print(f"Share link access error: {e}")
        await message.reply_text("⚠️ Failed to load shared file.")
