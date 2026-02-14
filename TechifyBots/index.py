from pyrogram import Client, filters
from vars import *
from Database.maindb import mdb
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio, re
from pyrogram.errors import FloodWait
from datetime import datetime

INDEX_TASKS = {}

# =========================================================
# SAVE MEDIA FUNCTION (Supports all types)
# =========================================================

async def save_media_message(message: Message):
    media = None
    media_type = None
    duration = 0

    if message.video:
        media = message.video
        media_type = "video"
        duration = media.duration or 0

    elif message.photo:
        media = message.photo[-1]
        media_type = "photo"

    elif message.document:
        media = message.document
        media_type = "document"

    elif message.audio:
        media = message.audio
        media_type = "audio"
        duration = media.duration or 0

    elif message.voice:
        media = message.voice
        media_type = "voice"
        duration = media.duration or 0

    if not media:
        return False

    if not await mdb.async_video_collection.find_one({"video_id": message.id}):
        await mdb.async_video_collection.insert_one({
            "video_id": message.id,
            "file_id": media.file_id,
            "media_type": media_type,
            "duration": duration,
            "added_at": datetime.now()
        })
        return True

    return False


# =========================================================
# AUTO INDEXING (All Media Types)
# =========================================================

@Client.on_message(
    filters.chat(DATABASE_CHANNEL_ID) &
    (filters.video | filters.photo | filters.document | filters.audio | filters.voice)
)
async def auto_index_media(client: Client, message: Message):
    try:
        await save_media_message(message)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await save_media_message(message)
    except Exception as e:
        print(f"Auto Index Error: {e}")


# =========================================================
# MANUAL INDEX COMMAND
# =========================================================

@Client.on_message(filters.command("index") & filters.private & filters.user(ADMIN_ID))
async def manual_index_cmd(client: Client, message: Message):

    channels = DATABASE_CHANNEL_ID
    if not isinstance(channels, list):
        channels = [channels]

    buttons = []
    for ch in channels:
        try:
            chat = await client.get_chat(ch)
            buttons.append(
                [InlineKeyboardButton(chat.title, callback_data=f"index_select_{ch}")]
            )
        except:
            continue

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")])

    await message.reply_text(
        "**Select Channel To Index:**",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# RECEIVE SKIP NUMBER
# =========================================================

@Client.on_message(filters.private & filters.user(ADMIN_ID) & filters.text)
async def receive_skip_number(client: Client, message: Message):

    if message.text.startswith("/"):
        return

    data = INDEX_TASKS.get(message.from_user.id)
    if not data or data.get("state") != "await_skip":
        return

    channel_id = data["channel_id"]
    text = message.text.strip()

    # Extract message ID
    if "t.me" in text:
        parts = text.strip("/").split("/")
        try:
            skip_id = int(parts[-1])
        except:
            return await message.reply_text("Invalid link.")
    else:
        if not text.isdigit():
            return await message.reply_text("Invalid message ID.")
        skip_id = int(text)

    await message.delete()
    msg_id = data.get("msg_id")
    progress_msg = await client.get_messages(message.chat.id, msg_id)
    await progress_msg.edit_text(
        "⏳ Starting Indexing...",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]
        )
    )

    INDEX_TASKS[message.from_user.id] = {
        "channel_id": channel_id,
        "skip_id": skip_id,
        "state": "indexing",
        "cancel": False,
        "progress_msg": progress_msg
    }

    asyncio.create_task(start_indexing(client, message.from_user.id))


# =========================================================
# INDEXING WORKER (FORWARD SAFE LOGIC)
# =========================================================

async def start_indexing(client: Client, user_id: int):

    print(f"[INDEX DEBUG] Starting indexing for user {user_id}")

    data = INDEX_TASKS.get(user_id)
    if not data:
        print("[INDEX DEBUG] No task data found")
        return

    channel_id = data["channel_id"]
    skip_id = data["skip_id"]
    progress_msg = data["progress_msg"]

    saved = 0
    duplicate = 0
    deleted = 0
    error = 0
    count = 0

    current_id = 1 if skip_id == 0 else skip_id + 1

    consecutive_missing = 0
    max_missing_limit = 100  # stop after 100 consecutive missing IDs

    while True:

        if data.get("cancel"):
            print("[INDEX DEBUG] Index cancelled")
            INDEX_TASKS.pop(user_id, None)
            return

        try:
            msg = await client.get_messages(channel_id, current_id)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            continue
        except:
            consecutive_missing += 1
            deleted += 1
            current_id += 1

            if consecutive_missing >= max_missing_limit:
                print("[INDEX DEBUG] Reached stop limit. Ending indexing.")
                break

            continue

        if not msg:
            consecutive_missing += 1
            current_id += 1

            if consecutive_missing >= max_missing_limit:
                print("[INDEX DEBUG] No more messages. Ending indexing.")
                break

            continue

        # Reset missing counter if message found
        consecutive_missing = 0

        try:
            inserted = await save_media_message(msg)
            if inserted:
                saved += 1
            else:
                duplicate += 1
        except Exception as e:
            error += 1
            print(f"[INDEX DEBUG] Save error: {e}")

        count += 1
        current_id += 1

        # Keep bot responsive
        if count % 50 == 0:
            await asyncio.sleep(0)

        # Update progress every 20
        if count % 20 == 0:
            try:
                await progress_msg.edit_text(
                    f"""📂 Indexing In Progress...

Processed: {count}

✅ Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted: {deleted}
⚠️ Errors: {error}
""",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]
                    )
                )
            except:
                pass

    # Final message
    try:
        await progress_msg.edit_text(
            f"""✅ Indexing Completed!

Total Processed: {count}

📁 Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted: {deleted}
⚠️ Errors: {error}
""",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]
            )
        )
    except:
        pass

    print("[INDEX DEBUG] Indexing finished")

    INDEX_TASKS.pop(user_id, None)
