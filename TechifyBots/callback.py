from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaVideo, InputMediaPhoto, InputMediaDocument, InputMediaAudio,
)
from pyrogram import Client
from Script import text
from vars import ADMIN_ID, ADMIN_IDS, DELETE_TIMER, PROTECT_CONTENT, IS_FSUB, CATEGORIES, PREMIUM_CAN_DOWNLOAD
from Database.maindb import mdb
from .cmds import (
    send_video, get_cached_user_data, get_bot_info,
    USER_ACTIVE_VIDEOS, USER_CURRENT_VIDEO,
    _build_category_markup, _categories_list_text, _make_file_buttons,
)
from .index import INDEX_TASKS, start_indexing
from .link_generator import handle_lg_callback
import asyncio, string, random
from datetime import datetime
from .fsub import get_fsub


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@Client.on_callback_query()
async def callback_query_handler(client, query: CallbackQuery):
    try:
        data = query.data
        uid = query.from_user.id

        # ── link-generator callbacks ──────────────────────────────────────
        if data.startswith("lg_"):
            await handle_lg_callback(client, query, data)
            return

        # ==================== GENERAL ====================

        if data == "start":
            await query.answer()
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
            await query.answer()
            await query.message.edit_caption(
                caption=text.HELP,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 𝖠𝖽𝗆𝗂𝗇 𝖢𝗈𝗆𝗆𝖺𝗇𝖽𝗌", callback_data="admincmds")],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                ])
            )

        elif data == "about":
            await query.answer()
            await query.message.edit_caption(
                caption=text.ABOUT,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍💻 𝖣𝖾𝗏𝖾𝗅𝗈𝗉𝖾𝗋 👨‍💻",
                                         user_id=int(ADMIN_ID) if isinstance(ADMIN_ID, int) else ADMIN_ID[0])],
                    [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                     InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                ])
            )

        elif data == "pro":
            await query.answer()
            current_limits = await mdb.get_global_limits()
            pro_text = text.PRO.format(free_limit=current_limits["free_limit"])
            admin_id_int = int(ADMIN_ID) if isinstance(ADMIN_ID, int) else ADMIN_ID[0]
            try:
                await query.message.edit_caption(
                    caption=pro_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Send Screenshot", user_id=admin_id_int)],
                        [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="start"),
                         InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                    ])
                )
            except Exception:
                await query.message.edit_text(
                    pro_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Send Screenshot", user_id=admin_id_int)],
                        [InlineKeyboardButton("❌ 𝖢𝗅𝗈𝗌𝖾", callback_data="close")],
                    ])
                )

        elif data == "admincmds":
            if not _is_admin(uid):
                await query.answer("You are not my admin ❌", show_alert=True)
            else:
                await query.answer()
                await query.message.edit_caption(
                    caption=text.ADMIN_COMMANDS,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("↩️ 𝖡𝖺𝖼𝗄", callback_data="help")]
                    ])
                )

        elif data == "getvideo":
            await query.answer()
            if IS_FSUB and not await get_fsub(client, query.message, user_id=uid):
                return
            # If this is a plain text message (category success msg), delete it first
            msg = query.message
            is_text_msg = not (msg.video or msg.photo or msg.document or msg.audio or msg.voice or msg.animation)
            await send_video(client, msg, uid=uid, delete_prev_msg=is_text_msg)

        elif data.startswith("prev_"):
            await query.answer()
            await handle_previous_video(client, query)

        elif data.startswith("share_"):
            await query.answer()
            await handle_share_video(client, query)

        elif data == "close":
            await query.answer()
            await query.message.delete()

        # ==================== CATEGORY ====================

        elif data == "show_category":
            # 📂 Category button pressed from under a file message.
            # Delete file message → show category picker as fresh text msg.
            await query.answer()

            if not CATEGORIES:
                await query.answer("📂 No categories configured yet.", show_alert=True)
                return

            user = await get_cached_user_data(uid)
            is_prime = user.get("plan") == "prime"
            current_cat = await mdb.get_user_category(uid)

            # Delete the file message first
            try:
                await query.message.delete()
            except Exception:
                pass

            if is_prime:
                markup = _build_category_markup(current_cat)
                markup.inline_keyboard.append([
                    InlineKeyboardButton("🎬 Get File", callback_data="getvideo"),
                    InlineKeyboardButton("❌ Close", callback_data="close"),
                ])
                await client.send_message(
                    query.message.chat.id,
                    f"📂 <b>Choose a Category</b>\n\n"
                    f"Current: <b>{'All' if current_cat == 'all' else current_cat}</b>\n\n"
                    f"<i>Tap a category to switch:</i>",
                    reply_markup=markup
                )
            else:
                admin_id_int = int(ADMIN_ID) if isinstance(ADMIN_ID, int) else ADMIN_ID[0]
                await client.send_message(
                    query.message.chat.id,
                    f"🔒 <b>Categories — Premium Only</b>\n\n"
                    f"<b>Available categories:</b>\n{_categories_list_text()}\n\n"
                    f"<i>Upgrade to Premium to select a specific category!</i>",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🍿 Buy Premium", callback_data="pro")],
                        [InlineKeyboardButton("💳 Contact Admin", user_id=admin_id_int)],
                    ])
                )

        elif data.startswith("cat_"):
            # Category button pressed — save choice and show success msg
            user = await get_cached_user_data(uid)
            is_prime = user.get("plan") == "prime"

            if not is_prime:
                await query.answer("🔒 This feature is for Premium users only!", show_alert=True)
                return

            chosen = data[4:]  # strip "cat_"
            if chosen != "all" and chosen not in CATEGORIES:
                await query.answer("❌ Invalid category.", show_alert=True)
                return

            await query.answer()
            await mdb.set_user_category(uid, chosen)
            cat_label = "All" if chosen == "all" else chosen

            # Edit same message to show success + Get File button
            success_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Get File", callback_data="getvideo")],
                [InlineKeyboardButton("🔄 Change Category", callback_data="show_category_from_text")],
                [InlineKeyboardButton("❌ Close", callback_data="close")],
            ])
            try:
                await query.message.edit_text(
                    f"✅ <b>Category set to: {cat_label}</b>\n\n"
                    f"<i>Tap below to get a file from this category!</i>",
                    reply_markup=success_markup
                )
            except Exception:
                pass

        elif data == "show_category_from_text":
            # "Change Category" from success message — edit in-place (no file to delete)
            await query.answer()

            if not CATEGORIES:
                await query.answer("📂 No categories configured yet.", show_alert=True)
                return

            user = await get_cached_user_data(uid)
            is_prime = user.get("plan") == "prime"
            current_cat = await mdb.get_user_category(uid)

            if is_prime:
                markup = _build_category_markup(current_cat)
                markup.inline_keyboard.append([
                    InlineKeyboardButton("🎬 Get File", callback_data="getvideo"),
                    InlineKeyboardButton("❌ Close", callback_data="close"),
                ])
                try:
                    await query.message.edit_text(
                        f"📂 <b>Choose a Category</b>\n\n"
                        f"Current: <b>{'All' if current_cat == 'all' else current_cat}</b>\n\n"
                        f"<i>Tap a category to switch:</i>",
                        reply_markup=markup
                    )
                except Exception:
                    pass
            else:
                admin_id_int = int(ADMIN_ID) if isinstance(ADMIN_ID, int) else ADMIN_ID[0]
                try:
                    await query.message.edit_text(
                        f"🔒 <b>Categories — Premium Only</b>\n\n"
                        f"<b>Available categories:</b>\n{_categories_list_text()}\n\n"
                        f"<i>Upgrade to Premium to select a specific category!</i>",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🍿 Buy Premium", callback_data="pro")],
                            [InlineKeyboardButton("💳 Contact Admin", user_id=admin_id_int)],
                        ])
                    )
                except Exception:
                    pass

        # ==================== INDEX ====================

        elif data.startswith("index_select_"):
            await query.answer()
            channel_id = int(data.split("_")[-1])
            try:
                await query.message.edit_text(
                    f"📂 **Channel selected:** `{channel_id}`\n\n"
                    f"Send the **message ID** to start indexing from (or `0` to start from beginning).\n\n"
                    f"You can also send a `t.me/...` link.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]
                    ])
                )
            except Exception:
                pass
            INDEX_TASKS[uid] = {
                "channel_id": channel_id,
                "state": "await_skip",
                "msg_id": query.message.id
            }

        elif data == "index_cancel":
            await query.answer("Cancelled.")
            task = INDEX_TASKS.get(uid)
            if task:
                task["cancel"] = True
            INDEX_TASKS.pop(uid, None)
            try:
                await query.message.edit_text("❌ **Indexing Cancelled.**")
            except Exception:
                pass

    except Exception as e:
        print(f"[callback_query_handler] error: {e}")


# ==================== PREVIOUS FILE HANDLER ====================

async def handle_previous_video(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    current_file_id = USER_CURRENT_VIDEO.get(user_id)
    if not current_file_id:
        await query.answer("❌ No current file found", show_alert=True)
        return

    prev_item = await mdb.get_previous_video(user_id, current_file_id)
    if not prev_item:
        await query.answer("❌ No previous file in history", show_alert=True)
        return

    user = await get_cached_user_data(user_id)
    is_prime = user.get("plan") == "prime"

    if is_prime:
        usage_text = "🌟 User Plan : Prime"
    else:
        user_data = await mdb.get_user(user_id)
        daily_count = user_data.get("daily_count", 0)
        limits = await mdb.get_global_limits()
        usage_text = f"📊 Daily Limit : {daily_count}/{limits['free_limit']}"

    cat_display = ""
    if is_prime:
        user_category = await mdb.get_user_category(user_id)
        cat_name = "All" if user_category == "all" else user_category
        cat_display = f"\n📂 Category: {cat_name}"

    mins = DELETE_TIMER // 60
    caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}{cat_display}</b>"

    history = await mdb.get_watch_history(user_id, limit=50)
    prev_file_id = prev_item["file_id"]
    media_type = prev_item.get("media_type", "video")
    USER_CURRENT_VIDEO[user_id] = prev_file_id

    current_index = next(
        (i for i, item in enumerate(history) if item["file_id"] == prev_file_id), None
    )
    has_previous = current_index is not None and current_index + 1 < len(history)

    protect = PROTECT_CONTENT
    if is_prime and PREMIUM_CAN_DOWNLOAD:
        protect = False

    buttons = _make_file_buttons(user_id, has_previous)
    markup = InlineKeyboardMarkup(buttons)

    try:
        if media_type == "video" and query.message.video:
            # Same type — edit in place
            await query.message.edit_media(
                InputMediaVideo(media=prev_file_id, caption=caption),
                reply_markup=markup,
            )
        else:
            # Different type or no video — delete and resend
            await query.message.delete()
            kwargs = dict(caption=caption, protect_content=protect, reply_markup=markup)
            if media_type == "video":
                await client.send_video(query.message.chat.id, prev_file_id, **kwargs)
            elif media_type == "photo":
                await client.send_photo(query.message.chat.id, prev_file_id, **kwargs)
            elif media_type == "audio":
                await client.send_audio(query.message.chat.id, prev_file_id, **kwargs)
            elif media_type in ("voice",):
                await client.send_voice(query.message.chat.id, prev_file_id, **kwargs)
            elif media_type == "animation":
                await client.send_animation(query.message.chat.id, prev_file_id, **kwargs)
            else:
                await client.send_document(query.message.chat.id, prev_file_id, **kwargs)
    except Exception as e:
        print(f"[handle_previous_video] error: {e}")
        await query.answer("⚠️ Failed to load previous file", show_alert=True)


# ==================== SHARE HANDLER ====================

async def handle_share_video(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    file_id = USER_CURRENT_VIDEO.get(user_id)
    if not file_id:
        await query.answer("❌ No current file found", show_alert=True)
        return

    link_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))

    # Detect media type from the message
    msg = query.message
    if msg.video:
        media_type = "video"
    elif msg.photo:
        media_type = "photo"
    elif msg.audio:
        media_type = "audio"
    elif msg.voice:
        media_type = "voice"
    elif msg.animation:
        media_type = "animation"
    else:
        media_type = "document"

    bot_info, _ = await asyncio.gather(
        get_bot_info(client),
        mdb.async_db["share_links"].insert_one({
            "link_id": link_id,
            "file_id": file_id,
            "media_type": media_type,
            "shared_by": user_id,
            "created_at": datetime.now(),
            "access_count": 0,
        }),
    )

    link = f"https://t.me/{bot_info.username}?start=share_{link_id}"
    await query.message.reply_text(
        f"🔗 **Share Link Generated!**\n\n`{link}`\n\nShare with your buddies 😉",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Copy Link", url=link)]]),
    )
    await query.answer("✅ Share link generated!", show_alert=False)


# ==================== SHARE LINK ACCESS ====================

async def handle_share_link_access(client, message, link_id: str):
    if IS_FSUB and not await get_fsub(client, message):
        return
    link_data = await mdb.async_db["share_links"].find_one({"link_id": link_id})
    if not link_data:
        await message.reply_text("❌ Invalid or expired share link.")
        return

    file_id = link_data["file_id"]
    media_type = link_data.get("media_type", "video")

    asyncio.create_task(
        mdb.async_db["share_links"].update_one(
            {"link_id": link_id}, {"$inc": {"access_count": 1}}
        )
    )

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Get More Files", callback_data="getvideo")]])
    caption = "🔗 **Shared file — tap below to get more!**"

    try:
        kwargs = dict(caption=caption, protect_content=PROTECT_CONTENT, reply_markup=markup)
        if media_type == "video":
            await client.send_video(message.chat.id, file_id, **kwargs)
        elif media_type == "photo":
            await client.send_photo(message.chat.id, file_id, **kwargs)
        elif media_type == "audio":
            await client.send_audio(message.chat.id, file_id, **kwargs)
        elif media_type in ("voice",):
            await client.send_voice(message.chat.id, file_id, **kwargs)
        elif media_type == "animation":
            await client.send_animation(message.chat.id, file_id, **kwargs)
        else:
            await client.send_document(message.chat.id, file_id, **kwargs)
    except Exception as e:
        print(f"[handle_share_link_access] error: {e}")
        await message.reply_text("⚠️ Failed to load shared file.")
