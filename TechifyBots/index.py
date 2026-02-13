from pyrogram import Client, filters
from vars import *
from Database.maindb import mdb
from pyrogram.types import Message
from pyrogram.types import *
import asyncio, re
from pyrogram.errors import FloodWait
import time

def debug_log(text):
    print(f"[INDEX DEBUG {time.strftime('%H:%M:%S')}] {text}")

INDEX_TASKS = {}

@Client.on_message(filters.chat(DATABASE_CHANNEL_ID) & filters.video)
async def save_video(client: Client, message: Message):
    try:
        video_id = message.id
        file_id = message.video.file_id
        video_duration = message.video.duration
        is_premium = False
        try:
            await mdb.save_video_id(video_id, file_id, video_duration, is_premium)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await mdb.save_video_id(video_id, file_id, video_duration, is_premium)
    except Exception as t:
        print(f"Auto Index Error: {str(t)}")

@Client.on_message(filters.command("index") & filters.private & filters.user(ADMIN_ID))
async def manual_index_cmd(client: Client, message: Message):
    debug_log(f"/index triggered by {message.from_user.id}")

    channels = DATABASE_CHANNEL_ID
    if not isinstance(channels, list):
        channels = [channels]

    buttons = []
    for ch in channels:
        try:
            chat = await client.get_chat(ch)
            debug_log(f"Adding button for channel: {ch}")
            buttons.append(
                [InlineKeyboardButton(chat.title, callback_data=f"index_select_{ch}")]
            )
        except Exception as e:
            debug_log(f"Error getting chat {ch}: {e}")

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")])

    await message.reply_text(
        "**Select Channel To Index:**",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@Client.on_callback_query(filters.regex("^index_select_"))
async def index_channel_selected(client: Client, callback_query: CallbackQuery):

    debug_log(f"Callback received: {callback_query.data}")
    debug_log(f"User ID: {callback_query.from_user.id}")

    await callback_query.answer()

    channel_id = int(callback_query.data.split("_")[-1])
    debug_log(f"Parsed channel_id: {channel_id}")

    try:
        await callback_query.message.edit_text(
            f"**Send Skip Message ID or Message Link**\n\nChannel: `{channel_id}`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]
            )
        )
        debug_log("Message edited successfully.")
    except Exception as e:
        debug_log(f"Edit failed: {e}")

    INDEX_TASKS[callback_query.from_user.id] = {
        "channel_id": channel_id,
        "state": "await_skip"
    }

    debug_log(f"State stored: {INDEX_TASKS.get(callback_query.from_user.id)}")


@Client.on_message(filters.private & filters.user(ADMIN_ID) & filters.text)
async def receive_skip_number(client: Client, message: Message):

    debug_log(f"Received message from admin: {message.text}")

    if message.text.startswith("/"):
        debug_log("Message is command. Ignored.")
        return

    data = INDEX_TASKS.get(message.from_user.id)
    debug_log(f"Current state for user: {data}")

    if not data or data.get("state") != "await_skip":
        debug_log("State not await_skip. Ignored.")
        return

    debug_log("State matched. Proceeding to skip parsing.")

@Client.on_callback_query(filters.regex("^index_cancel$"))
async def cancel_indexing(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()
    user_id = callback_query.from_user.id
    task = INDEX_TASKS.get(user_id)
    if task:
        task["cancel"] = True
    try:
        await callback_query.message.edit_text("❌ Indexing Cancelled.")
    except:
        pass

async def start_indexing(client: Client, user_id: int):
    data = INDEX_TASKS.get(user_id)
    if not data:
        return

    channel_id = data["channel_id"]
    skip_id = data["skip_id"]
    progress_msg = data["progress_msg"]

    saved = 0
    duplicate = 0
    deleted = 0
    error = 0
    count = 0

    try:
        async for msg in client.get_chat_history(
                channel_id,
                offset_id=skip_id,
                reverse=True
        ):

            if data["cancel"]:
                INDEX_TASKS.pop(user_id, None)
                return

            if msg.empty:
                deleted += 1
                continue
   
            if not msg.video:
                continue

            try:
                existing = await mdb.async_video_collection.find_one(
                    {"video_id": msg.id}
                )
                if existing:
                    duplicate += 1
                else:
                    await mdb.save_video_id(
                        msg.id,
                        msg.video.file_id,
                        msg.video.duration,
                        False
                    )
                    saved += 1

            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue

            except Exception:
                error += 1

            count += 1

            # Progress update every 20 files
            if count % 20 == 0:
                try:
                    await progress_msg.edit_text(
                        f"""📂 Indexing In Progress...

Processed: {count}

✅ Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted/Not Exist: {deleted}
⚠️ Errors: {error}
"""
                    )
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except:
                    pass

    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await start_indexing(client, user_id)

    except Exception as e:
        print(f"Indexing Fatal Error: {e}")

    # Final message (always executes)
    try:
        await progress_msg.edit_text(
            f"""✅ Indexing Completed!

Total Processed: {count}

📁 Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted/Not Exist: {deleted}
⚠️ Errors: {error}
"""
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await progress_msg.edit_text(
            f"""✅ Indexing Completed!

Total Processed: {count}

📁 Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted/Not Exist: {deleted}
⚠️ Errors: {error}
"""
        )
    except:
        pass

    INDEX_TASKS.pop(user_id, None)



