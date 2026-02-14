from pyrogram import Client, filters
from vars import *
from Database.maindb import mdb
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
from pyrogram.errors import FloodWait
from datetime import datetime

INDEX_TASKS = {}

# ==================== SAVE MEDIA ====================

async def save_media(msg: Message):
    """Save any media type"""
    media = None
    media_type = None
    duration = 0

    if msg.video:
        media, media_type, duration = msg.video, "video", msg.video.duration or 0
    elif msg.photo:
        media, media_type = msg.photo, "photo"
    elif msg.document:
        media, media_type = msg.document, "document"
    elif msg.audio:
        media, media_type, duration = msg.audio, "audio", msg.audio.duration or 0
    elif msg.voice:
        media, media_type, duration = msg.voice, "voice", msg.voice.duration or 0

    if not media:
        return False

    if not await mdb.async_video_collection.find_one({"video_id": msg.id}):
        await mdb.async_video_collection.insert_one({
            "video_id": msg.id,
            "file_id": media.file_id,
            "media_type": media_type,
            "duration": duration,
            "added_at": datetime.now()
        })
        return True
    return False

# ==================== AUTO INDEX (MULTI-CHANNEL) ====================

# Convert single channel to list for filter
CHANNEL_LIST = DATABASE_CHANNEL_ID if isinstance(DATABASE_CHANNEL_ID, list) else [DATABASE_CHANNEL_ID]

@Client.on_message(
    filters.chat(CHANNEL_LIST) &
    (filters.video | filters.photo | filters.document | filters.audio | filters.voice)
)
async def auto_index(client: Client, message: Message):
    """Auto-index from all database channels"""
    try:
        await save_media(message)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await save_media(message)
    except Exception as e:
        print(f"Auto index error: {e}")

# ==================== MANUAL INDEX ====================

@Client.on_message(filters.command("index") & filters.private & filters.user(ADMIN_ID))
async def manual_index(client: Client, message: Message):
    """Select channel to index"""
    channels = CHANNEL_LIST
    buttons = []
    
    for ch in channels:
        try:
            chat = await client.get_chat(ch)
            buttons.append([InlineKeyboardButton(chat.title, callback_data=f"index_select_{ch}")])
        except:
            continue
    
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")])
    await message.reply_text("**Select Channel:**", reply_markup=InlineKeyboardMarkup(buttons))

# ==================== SKIP NUMBER ====================

@Client.on_message(filters.private & filters.user(ADMIN_ID) & filters.text)
async def skip_number(client: Client, message: Message):
    """Receive skip message ID"""
    if message.text.startswith("/"):
        return
    
    data = INDEX_TASKS.get(message.from_user.id)
    if not data or data.get("state") != "await_skip":
        return
    
    channel_id = data["channel_id"]
    text = message.text.strip()
    
    # Extract ID from link or number
    if "t.me" in text:
        try:
            skip_id = int(text.strip("/").split("/")[-1])
        except:
            return await message.reply_text("Invalid link")
    else:
        if not text.isdigit():
            return await message.reply_text("Invalid ID")
        skip_id = int(text)
    
    await message.delete()
    progress = await client.get_messages(message.chat.id, data["msg_id"])
    await progress.edit_text(
        "⏳ Starting...",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]])
    )
    
    INDEX_TASKS[message.from_user.id] = {
        "channel_id": channel_id,
        "skip_id": skip_id,
        "state": "indexing",
        "cancel": False,
        "progress_msg": progress
    }
    
    asyncio.create_task(start_indexing(client, message.from_user.id))

# ==================== INDEXING WORKER ====================

async def start_indexing(client: Client, user_id: int):
    """Index channel messages"""
    data = INDEX_TASKS.get(user_id)
    if not data:
        return
    
    channel_id = data["channel_id"]
    skip_id = data["skip_id"]
    progress = data["progress_msg"]
    
    saved = duplicate = deleted = error = count = 0
    current_id = 1 if skip_id == 0 else skip_id + 1
    consecutive_missing = 0
    max_missing = 100
    
    while True:
        if data.get("cancel"):
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
            if consecutive_missing >= max_missing:
                break
            continue
        
        if not msg or msg.empty:
            consecutive_missing += 1
            deleted += 1
            current_id += 1
            if consecutive_missing >= max_missing:
                break
            continue
        
        consecutive_missing = 0
        
        try:
            if await save_media(msg):
                saved += 1
            else:
                duplicate += 1
        except:
            error += 1
        
        count += 1
        current_id += 1
        
        if count % 50 == 0:
            await asyncio.sleep(0)
        
        if count % 20 == 0:
            try:
                await progress.edit_text(
                    f"""📂 Indexing...

Processed: {count}
✅ Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted: {deleted}
⚠️ Errors: {error}

ID: {current_id - 1}""",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]])
                )
            except:
                pass
    
    try:
        await progress.edit_text(
            f"""✅ Complete!

Total: {count}
📁 Saved: {saved}
♻️ Duplicate: {duplicate}
❌ Deleted: {deleted}
⚠️ Errors: {error}""",
            reply_markup=None
        )
    except:
        pass
    
    INDEX_TASKS.pop(user_id, None)
