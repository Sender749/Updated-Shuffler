from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from pyrogram import Client
from Script import text
from vars import ADMIN_ID, DELETE_TIMER, PROTECT_CONTENT, IS_FSUB
from Database.maindb import mdb
from .cmds import send_video, get_cached_user_data, USER_ACTIVE_VIDEOS, USER_CURRENT_VIDEO
from .index import INDEX_TASKS, start_indexing
from .link_generator import (SCREENSHOT_SESSIONS, SS_CANCEL_FLAGS, SS_BG_TASKS, SS_DL_CUSTOM_MSGS, show_screenshot, generate_screenshots, post_screenshot_to_channel,)
import asyncio, string, random, os
from datetime import datetime
from .fsub import get_fsub

@Client.on_callback_query()
async def callback_query_handler(client, query: CallbackQuery):
    try:
        data = query.data

        # ==================== GENERAL ====================

        if data == "start":
            try:
                await query.message.edit_caption(
                    caption=text.START.format(query.from_user.mention),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")],
                        [InlineKeyboardButton("🍿 𝖡𝗎𝗒 𝖲𝗎𝖻𝗌𝖼𝗋𝗂𝗉𝗍𝗂𝗈𝗇 🍾", callback_data="pro")],
                        [InlineKeyboardButton("ℹ️ Disclaimer", callback_data="about"),
                         InlineKeyboardButton("📚 𝖧𝖾𝗅𝗉", callback_data="help")],
                    ])
                )
            except Exception:
                pass

        elif data == "help":
            await query.message.edit_caption(
                caption=text.HELP,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 𝖠𝖽𝗆𝗂𝗇 𝖢𝗈𝗆𝗆𝖺𝗇𝖽𝗌", callback_data="admincmds")],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                ])
            )

        elif data == "about":
            await query.message.edit_caption(
                caption=text.ABOUT,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍💻 𝖣𝖾𝗏𝖾𝗅𝗈𝗉𝖾𝗋 👨‍💻", user_id=int(ADMIN_ID))],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                ])
            )

        elif data == "pro":
            current_limits = await mdb.get_global_limits()
            pro_text = text.PRO.format(free_limit=current_limits["free_limit"])
            await query.message.edit_caption(
                caption=pro_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Send Screenshot", user_id=int(ADMIN_ID))],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                ])
            )

        elif data == "admincmds":
            if query.from_user.id != ADMIN_ID:
                await query.answer("You are not my admin ❌", show_alert=True)
            else:
                await query.message.edit_caption(
                    caption=text.ADMIN_COMMANDS,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="help")]
                    ])
                )

        elif data == "getvideo":
            await query.answer()
            # Check force sub before sending video (pass user_id since query.message has no from_user)
            if IS_FSUB and not await get_fsub(client, query.message, user_id=query.from_user.id):
                return
            await send_video(client, query.message, uid=query.from_user.id)

        elif data.startswith("prev_"):
            await query.answer()
            await handle_previous_video(client, query)

        elif data.startswith("share_"):
            await query.answer()
            await handle_share_video(client, query)

        elif data == "close":
            await query.message.delete()

        # ==================== INDEX ====================

        elif data.startswith("index_select_"):
            await query.answer()
            channel_id = int(data.split("_")[-1])
            try:
                await query.message.edit_text(
                    f"**Send Skip Message ID or Message Link**\n\nChannel: `{channel_id}`",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]
                    ])
                )
            except Exception:
                pass
            INDEX_TASKS[query.from_user.id] = {
                "channel_id": channel_id, "state": "await_skip", "msg_id": query.message.id
            }

        elif data == "index_cancel":
            await query.answer()
            task = INDEX_TASKS.get(query.from_user.id)
            if task:
                task["cancel"] = True
            try:
                await query.message.edit_text("❌ Indexing Cancelled.")
            except Exception:
                pass

        # ==================== SCREENSHOT NAVIGATION ====================

        elif data == "ss_noop":
            await query.answer()

        elif data.startswith("ss_next_") or data.startswith("ss_back_"):
            await query.answer()
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.rsplit("_", 1)[1])
            ss = SCREENSHOT_SESSIONS.get(uid)
            if not ss:
                await query.answer("❌ Session expired. Use /l to start again.", show_alert=True)
                return
            total = len(ss["screenshots"])
            if data.startswith("ss_next_"):
                ss["current_index"] = (ss["current_index"] + 1) % total
            else:
                ss["current_index"] = (ss["current_index"] - 1) % total
            await show_screenshot(client, query.message.chat.id, uid)

        elif data.startswith("ss_custom_"):
            await query.answer()
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_custom_")[1])
            ss = SCREENSHOT_SESSIONS.get(uid)
            if not ss:
                await query.answer("❌ Session expired.", show_alert=True)
                return
            ss["state"] = "awaiting_custom_photo"
            # Send a NEW message asking for photo/video (nav message stays intact for Back)
            try:
                sent = await query.message.reply_text(
                    "📸 **Send a photo or video** to use as the custom screenshot/thumbnail.\n\n"
                    "• **Photo** → used directly as screenshot\n"
                    "• **Video** → first frame extracted as screenshot\n\n"
                    "Click **Back** to return to the screenshot preview.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Back", callback_data=f"ss_custom_back_{uid}")]
                    ])
                )
                ss["custom_ask_msg_id"] = sent.id
                ss["custom_ask_chat_id"] = query.message.chat.id
            except Exception:
                pass

        elif data.startswith("ss_gen_"):
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_gen_")[1])
            ss = SCREENSHOT_SESSIONS.get(uid)
            if not ss:
                await query.answer("❌ Session expired.", show_alert=True)
                return
            await query.answer("🔄 Generating more screenshots…")
            # Edit the nav message as status display
            try:
                await query.message.edit_caption(
                    "⏳ **Generating more screenshots…**\n\nPlease wait.",
                    reply_markup=None,
                )
            except Exception:
                pass
            try:
                new_shots = await generate_screenshots(
                    client,
                    ss["source_files"],
                    ss["used_timestamps"],
                    max_shots=20,
                )
                if new_shots:
                    ss["screenshots"].extend(new_shots)
                    ss["current_index"] = len(ss["screenshots"]) - len(new_shots)
                    # nav_msg_id is still valid; show_screenshot will edit it
                    await show_screenshot(client, query.message.chat.id, uid)
                else:
                    # No new shots — restore navigator
                    await show_screenshot(client, query.message.chat.id, uid)
                    await query.answer("⚠️ No new unique frames found.", show_alert=True)
            except Exception as e:
                print(f"[ss_gen] error: {e}")
                await show_screenshot(client, query.message.chat.id, uid)

        elif data.startswith("ss_send_"):
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_send_")[1])
            await post_screenshot_to_channel(client, query.message.chat.id, uid, query=query)

        # ==================== DOWNLOAD-STAGE CANCEL & CUSTOM ====================

        elif data.startswith("ss_dl_cancel_"):
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_dl_cancel_")[1])
            # Set the cancel flag — the generator will check this
            SS_CANCEL_FLAGS[uid] = True
            await query.answer("❌ Cancelling…", show_alert=False)
            try:
                await query.message.edit_text("❌ Cancelling screenshot generation…")
            except Exception:
                pass

        elif data.startswith("ss_dl_custom_"):
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_dl_custom_")[1])
            await query.answer("📸 Send your photo or video", show_alert=False)

            # Don't stop generation — it continues in background
            # Send a new message asking for photo or video with a Back button
            try:
                sent = await query.message.reply_text(
                    "📸 **Send a photo or video** to use for the channel post.\n\n"
                    "• **Photo** → posted directly\n"
                    "• **Video** → first frame extracted and posted\n\n"
                    "The bot will continue downloading & generating screenshots in the background.\n"
                    "Click **Back** to return and wait for automatic screenshot generation.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Back", callback_data=f"ss_dl_custom_back_{uid}")]
                    ])
                )
                # Store so media handler can delete it after use
                SS_DL_CUSTOM_MSGS[uid] = {
                    "msg_id": sent.id,
                    "chat_id": query.message.chat.id,
                }
            except Exception:
                pass

        # ==================== CUSTOM BACK BUTTONS ====================

        elif data.startswith("ss_custom_back_"):
            # Back from custom photo/video ask during SS preview phase
            await query.answer()
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_custom_back_")[1])
            ss = SCREENSHOT_SESSIONS.get(uid)
            if not ss:
                await query.answer("❌ Session expired.", show_alert=True)
                return
            ss["state"] = "browsing"
            # Delete the ask message
            try:
                await query.message.delete()
            except Exception:
                pass
            # The nav message (SS preview) is still intact — just answer

        elif data.startswith("ss_dl_custom_back_"):
            # Back from custom photo/video ask during download phase
            await query.answer()
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_dl_custom_back_")[1])
            # Remove the pending custom entry so media handler won't intercept
            SS_DL_CUSTOM_MSGS.pop(uid, None)
            # Delete this ask message
            try:
                await query.message.delete()
            except Exception:
                pass
            # Generation is still running in background — nothing else to do

        # ==================== CANCEL POST (screenshot preview) ====================

        elif data.startswith("ss_cancel_post_"):
            if query.from_user.id != ADMIN_ID:
                await query.answer("❌ Not allowed", show_alert=True)
                return
            uid = int(data.split("ss_cancel_post_")[1])
            ss = SCREENSHOT_SESSIONS.get(uid)
            if not ss:
                await query.answer("❌ No active session.", show_alert=True)
                return
            # Delete the link from DB
            link_id = ss.get("link_id")
            if link_id:
                try:
                    from Database.maindb import mdb
                    await mdb.async_db["file_links"].delete_one({"link_id": link_id})
                except Exception as e:
                    print(f"[ss_cancel_post] db delete error: {e}")
            # Clean up temp files
            screenshots = ss.get("screenshots", [])
            temp_dirs = set()
            for path in screenshots:
                try:
                    if os.path.exists(path):
                        temp_dirs.add(os.path.dirname(path))
                except Exception:
                    pass
            for d in temp_dirs:
                try:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
            # Delete the navigator message
            nav_msg_id = ss.get("nav_msg_id")
            nav_chat_id = ss.get("nav_chat_id")
            if nav_msg_id:
                try:
                    await client.delete_messages(nav_chat_id, nav_msg_id)
                except Exception:
                    pass
            SCREENSHOT_SESSIONS.pop(uid, None)
            await query.answer("✅ Post cancelled and deleted from DB.", show_alert=True)

    except Exception as e:
        print(f"[callback_query_handler] error: {e}")
        try:
            await query.answer("⚠️ An error occurred. Try again.", show_alert=True)
        except Exception:
            pass


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
        from Database.userdb import udb  # noqa
        user_data = await mdb.get_user(user_id)
        daily_count = user_data.get("daily_count", 0)
        limits = await mdb.get_global_limits()
        usage_text = f"📊 Daily Limit : {daily_count}/{limits['free_limit']}"

    mins = DELETE_TIMER // 60
    caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}</b>"

    history = await mdb.get_watch_history(user_id, limit=50)
    prev_file_id = prev_video["file_id"]
    USER_CURRENT_VIDEO[user_id] = prev_file_id

    current_index = next(
        (i for i, item in enumerate(history) if item["file_id"] == prev_file_id), None
    )
    has_previous = current_index is not None and current_index + 1 < len(history)

    buttons = []
    if has_previous:
        buttons.append([
            InlineKeyboardButton("⬅️ Back", callback_data=f"prev_{user_id}"),
            InlineKeyboardButton("➡️ Next", callback_data="getvideo"),
        ])
    else:
        buttons.append([InlineKeyboardButton("➡️ Next", callback_data="getvideo")])
    buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{user_id}")])

    try:
        media_type = prev_video.get("media_type", "video")
        if media_type == "video":
            await query.message.edit_media(
                InputMediaVideo(media=prev_file_id, caption=caption),
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await query.message.delete()
            if media_type == "photo":
                await client.send_photo(
                    query.message.chat.id, prev_file_id, caption=caption,
                    protect_content=PROTECT_CONTENT,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            else:
                await client.send_document(
                    query.message.chat.id, prev_file_id, caption=caption,
                    protect_content=PROTECT_CONTENT,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
    except Exception as e:
        print(f"[handle_previous_video] error: {e}")
        await query.answer("⚠️ Failed to load previous video", show_alert=True)


# ==================== SHARE VIDEO HANDLER ====================

async def handle_share_video(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    file_id = USER_CURRENT_VIDEO.get(user_id)
    if not file_id:
        await query.answer("❌ No current video found", show_alert=True)
        return

    link_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    media_type = "video"
    if query.message.photo:
        media_type = "photo"
    elif query.message.document:
        media_type = "document"

    await mdb.async_db["share_links"].insert_one({
        "link_id": link_id,
        "file_id": file_id,
        "media_type": media_type,
        "shared_by": user_id,
        "created_at": datetime.now(),
        "access_count": 0,
    })
    bot_info = await client.get_me()
    link = f"https://t.me/{bot_info.username}?start=share_{link_id}"

    await query.message.reply_text(
        f"🔗 **Share Link Generated!**\n\n`{link}`\n\nShare with your buddies 😉",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Copy Link", url=link)]]),
    )
    await query.answer("✅ Share link generated!", show_alert=False)


# ==================== HANDLE SHARE LINK ACCESS ====================

async def handle_share_link_access(client, message, link_id: str):
    if IS_FSUB and not await get_fsub(client, message):
        return
    link_data = await mdb.async_db["share_links"].find_one({"link_id": link_id})
    if not link_data:
        await message.reply_text("❌ Invalid or expired share link.")
        return

    file_id = link_data["file_id"]
    media_type = link_data["media_type"]

    await mdb.async_db["share_links"].update_one(
        {"link_id": link_id}, {"$inc": {"access_count": 1}}
    )

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Get More Videos", callback_data="getvideo")
    ]])
    caption = "🔗 **Click below button to get more videos❗**"

    try:
        if media_type == "video":
            await client.send_video(
                message.chat.id, file_id, caption=caption,
                protect_content=PROTECT_CONTENT, reply_markup=markup,
            )
        elif media_type == "photo":
            await client.send_photo(
                message.chat.id, file_id, caption=caption,
                protect_content=PROTECT_CONTENT, reply_markup=markup,
            )
        else:
            await client.send_document(
                message.chat.id, file_id, caption=caption,
                protect_content=PROTECT_CONTENT, reply_markup=markup,
            )
    except Exception as e:
        print(f"[handle_share_link_access] error: {e}")
        await message.reply_text("⚠️ Failed to load shared file.")
