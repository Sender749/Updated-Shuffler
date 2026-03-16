from pyrogram.types import *
from Database.userdb import udb
from Database.maindb import mdb
from vars import ADMIN_IDS
import asyncio
from pyrogram.errors import *
from pyrogram import *
from bot import bot
import time
import re


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def parse_button_markup(text: str):
    lines = text.split("\n")
    buttons = []
    final_text_lines = []
    for line in lines:
        row = []
        parts = line.split("||")
        is_button_line = True
        for part in parts:
            match = re.fullmatch(r"\[(.+?)\]\((https?://[^\s]+)\)", part.strip())
            if match:
                row.append(InlineKeyboardButton(match[1], url=match[2]))
            else:
                is_button_line = False
                break
        if is_button_line and row:
            buttons.append(row)
        else:
            final_text_lines.append(line)
    return InlineKeyboardMarkup(buttons) if buttons else None, "\n".join(final_text_lines).strip()


async def get_readable_time(seconds: int) -> str:
    time_data = []
    for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        value, seconds = divmod(seconds, div)
        if value > 0 or unit == "s":
            time_data.append(f"{int(value)}{unit}")
    return " ".join(time_data)


@Client.on_message(filters.command("stats") & filters.private)
async def stats_command(client, message):
    if not is_admin(message.from_user.id):
        await message.delete()
        await message.reply_text("**🚫 You're not authorized to use this command...**")
        return

    loading = await message.reply_text("⏳ Fetching stats...")

    from vars import DATABASE_CHANNEL_ID, CATEGORIES
    channel_list = DATABASE_CHANNEL_ID if isinstance(DATABASE_CHANNEL_ID, list) else [DATABASE_CHANNEL_ID]

    # Gather totals concurrently
    total_files, total_users, premium_users = await asyncio.gather(
        mdb.count_all_videos(),
        udb.get_all_users(),
        mdb.get_all_premium_users(),
    )

    bot_uptime = int(time.time() - bot.START_TIME)
    uptime = await get_readable_time(bot_uptime)

    # Build channel stats — name + count per channel
    channel_lines = []
    for ch_id in channel_list:
        try:
            chat = await client.get_chat(ch_id)
            ch_name = chat.title or str(ch_id)
        except Exception:
            ch_name = str(ch_id)
        count = await mdb.async_video_collection.count_documents({"source_channel_id": ch_id})
        channel_lines.append(f"  • **{ch_name}**: `{count}` files")

    channel_block = "\n".join(channel_lines) if channel_lines else "  _No channels configured_"

    STATS  = ">**📊 Bot Statistics**\n\n"
    STATS += f"**👥 Total Users:** `{len(total_users)}`\n"
    STATS += f"**👑 Premium Users:** `{len(premium_users)}`\n"
    STATS += f"**🗂 Total Files in DB:** `{total_files}`\n"
    STATS += f"**⏱ Bot Uptime:** `{uptime}`\n"
    STATS += f"\n**📡 Channel Breakdown:**\n{channel_block}"

    await loading.edit_text(STATS)


@Client.on_message(filters.command("broadcast") & filters.private)
async def broadcasting_func(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.reply_to_message:
        return await message.reply("<b>Reply to a message to broadcast.</b>")
    msg = await message.reply_text("Processing broadcast...")
    to_copy_msg = message.reply_to_message
    users_list = await udb.get_all_users()
    completed = 0
    failed = 0
    raw_text = to_copy_msg.caption or to_copy_msg.text or ""
    reply_markup, cleaned_text = parse_button_markup(raw_text)
    for i, user in enumerate(users_list):
        user_id = user.get("user_id")
        if not user_id:
            continue
        try:
            if to_copy_msg.text:
                await client.send_message(user_id, cleaned_text, reply_markup=reply_markup)
            elif to_copy_msg.photo:
                await client.send_photo(user_id, to_copy_msg.photo.file_id, caption=cleaned_text, reply_markup=reply_markup)
            elif to_copy_msg.video:
                await client.send_video(user_id, to_copy_msg.video.file_id, caption=cleaned_text, reply_markup=reply_markup)
            elif to_copy_msg.document:
                await client.send_document(user_id, to_copy_msg.document.file_id, caption=cleaned_text, reply_markup=reply_markup)
            else:
                await to_copy_msg.copy(user_id)
            completed += 1
        except (UserIsBlocked, PeerIdInvalid, InputUserDeactivated):
            await udb.unban_user(user_id)
            failed += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await to_copy_msg.copy(user_id)
                completed += 1
            except:
                failed += 1
        except Exception as e:
            print(f"Broadcast to {user_id} failed: {e}")
            failed += 1
        await msg.edit(f"Total: {i + 1}\nCompleted: {completed}\nFailed: {failed}")
        await asyncio.sleep(0.1)
    await msg.edit(
        f"😶‍🌫 <b>Broadcast Completed</b>\n\n👥 Total Users: <code>{len(users_list)}</code>\n✅ Successful: <code>{completed}</code>\n🤯 Failed: <code>{failed}</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎭 𝖢𝗅𝗈𝗌𝖾", callback_data="close")]])
    )


@Client.on_message(filters.command("ban") & filters.private)
async def ban_user_cmd(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        command_parts = message.text.split()
        if len(command_parts) < 2:
            await message.reply_text("Usage: /ban user_id")
            return
        user_id = int(command_parts[1])
        reason = " ".join(command_parts[2:]) if len(command_parts) > 2 else None
        try:
            user = await client.get_users(user_id)
        except Exception:
            await message.reply_text("Unable to find user.")
            return
        if await udb.ban_user(user_id, reason):
            ban_message = f"User {user.mention} has been banned."
            if reason:
                ban_message += f"\nReason: {reason}"
            await message.reply_text(ban_message)
        else:
            await message.reply_text("Failed to ban user.")
    except ValueError:
        await message.reply_text("Please provide a valid user ID.")
    except Exception as e:
        await message.reply_text(f"An error occurred: {str(e)}")


@Client.on_message(filters.command("maintenance") & filters.private)
async def maintenance_mode(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /maintenance [on/off]")
            return
        status = args[1].lower()
        if status not in ["on", "off"]:
            await message.reply_text("Invalid status. Use 'on' or 'off'")
            return
        await mdb.set_maintenance_status(status == "on")
        await message.reply_text(f"Maintenance mode {'activated' if status == 'on' else 'deactivated'}")
    except Exception as e:
        await message.reply_text(f"Error: {str(e)}")


@Client.on_message(filters.command("unban") & filters.private)
async def unban_user_cmd(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        command_parts = message.text.split()
        if len(command_parts) < 2:
            await message.reply_text("Usage: /unban user_id")
            return
        user_id = int(command_parts[1])
        try:
            user = await client.get_users(user_id)
        except Exception:
            await message.reply_text("Unable to find user.")
            return
        if await udb.unban_user(user_id):
            await message.reply_text(f"User {user.mention} has been unbanned.")
        else:
            await message.reply_text("Failed to unban user or user was not banned.")
    except ValueError:
        await message.reply_text("Please provide a valid user ID.")
    except Exception as e:
        await message.reply_text(f"An error occurred: {str(e)}")


@Client.on_message(filters.command("banlist") & filters.private)
async def banlist(client, message):
    if not is_admin(message.from_user.id):
        return
    response = await message.reply("<b>Fetching banned users...</b>")
    try:
        banned_users = await udb.banned_users.find().to_list(length=None)
        if not banned_users:
            return await response.edit("<b>No users are currently banned.</b>")
        text = "<b>🚫 Banned Users:</b>\n\n"
        for user in banned_users:
            user_id = user.get("user_id")
            reason = user.get("reason", "No reason provided")
            text += f"• <code>{user_id}</code> — {reason}\n"
        await response.edit(text)
    except Exception as e:
        await response.edit(f"<b>Error:</b> <code>{str(e)}</code>")


@Client.on_message(filters.command("deleteall") & filters.private)
async def delete_all_videos_command(client, message):
    if not is_admin(message.from_user.id):
        return
    try:
        t = await message.reply_text("**Proceed to delete all videos ♻️**")
        await mdb.delete_all_videos()
        await t.edit_text("**✅ All videos have been deleted from the database**")
    except Exception as e:
        await message.reply_text(f"**Error: {str(e)}**")


@Client.on_message(filters.command("delete") & filters.private)
async def delete_video_by_id_command(client, message):
    if not is_admin(message.from_user.id):
        return
    if len(message.command) < 2:
        await message.reply_text(
            "Usage:\n"
            "/delete <video_id> — integer video ID\n"
            "/delete <post_id>  — post ID shown in channel caption\n"
            "/delete <link_id>  — internal link ID"
        )
        return

    target = message.command[1].strip()

    try:
        video_id = int(target)
        deleted = await mdb.delete_video_by_id(video_id)
        if deleted:
            await message.reply_text(f"Deleted video ID `{video_id}` from videos DB.")
            return
    except ValueError:
        pass

    r1 = await mdb.async_db["file_links"].delete_one({"post_id": target})
    if r1.deleted_count:
        await message.reply_text(f"Deleted link set with Post ID `{target}`.")
        return

    r2 = await mdb.async_db["file_links"].delete_one({"link_id": target})
    if r2.deleted_count:
        await message.reply_text(f"Deleted link with Link ID `{target}`.")
        return

    await message.reply_text(f"No record found for ID `{target}`.")
