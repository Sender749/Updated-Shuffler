from pyrogram import Client, filters
from vars import *
from Database.maindb import mdb
from pyrogram.types import *
import asyncio, time, re
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from vars import ADMIN_ID, DATABASE_CHANNEL_ID

# Global variables for indexing state
indexing_lock = asyncio.Lock()
CANCEL_INDEXING = False

@Client.on_message(filters.chat(DATABASE_CHANNEL_ID) & filters.video)
async def save_video(client: Client, message: Message):
    try:
        video = message.video
        video_id = message.id
        channel_id = message.chat.id
        duration = video.duration or 0
        result = await mdb.save_video_id(video_id, channel_id, duration)
        if result == 'suc':
            print(f"✅ Auto-indexed video {video_id} from channel {channel_id}")
        elif result == 'dup':
            print(f"🔁 Duplicate video {video_id} from channel {channel_id}")
        elif result == 'err':
            print(f"❌ Error indexing video {video_id} from channel {channel_id}")
    except Exception as e:
        print(f"Error in auto-indexing: {e}")

def get_readable_time(seconds: int) -> str:
    """Convert seconds to readable time format"""
    time_data = []
    for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        value, seconds = divmod(seconds, div)
        if value > 0 or unit == "s":
            time_data.append(f"{int(value)}{unit}")
    return " ".join(time_data)

@Client.on_message(filters.command('index') & filters.private & filters.user(ADMIN_ID))
async def index_command(client, message: Message):
    global CANCEL_INDEXING
    
    if indexing_lock.locked():
        return await message.reply('**⚠️ Please wait until the previous indexing process completes.**')
    
    # Show channel list
    if len(DATABASE_CHANNEL_ID) == 0:
        return await message.reply("**❌ No database channels configured in DATABASE_CHANNEL_ID**")
    
    buttons = []
    for channel_id in DATABASE_CHANNEL_ID:
        try:
            chat = await client.get_chat(channel_id)
            channel_name = chat.title or f"Channel {channel_id}"
            buttons.append([InlineKeyboardButton(
                f"📁 {channel_name}", 
                callback_data=f"index_channel_{channel_id}"
            )])
        except Exception as e:
            print(f"Error getting channel {channel_id}: {e}")
            buttons.append([InlineKeyboardButton(
                f"📁 Channel {channel_id} (Access Error)", 
                callback_data=f"index_channel_{channel_id}"
            )])
    
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")])
    
    await message.reply(
        "**📚 Select a channel to index:**\n\nChoose the channel from which you want to save videos to the database.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@Client.on_callback_query(filters.regex(r'^index_channel_'))
async def index_channel_selected(client, query):
    global CANCEL_INDEXING
    
    if query.from_user.id != ADMIN_ID:
        return await query.answer("⛔ You are not authorized!", show_alert=True)
    
    channel_id = int(query.data.split("_")[-1])
    
    try:
        chat = await client.get_chat(channel_id)
        channel_name = chat.title or f"Channel {channel_id}"
    except Exception as e:
        return await query.message.edit_text(f"**❌ Error accessing channel: {str(e)}**")
    
    await query.message.edit_text(
        f"**📁 Selected Channel:** {channel_name}\n\n"
        f"**Please send:**\n"
        f"1. A number (e.g., `50`) to skip first 50 messages\n"
        f"2. Or a message link to start from that message\n"
        f"3. Or `0` to start from the beginning"
    )
    
    # Store channel_id in message for later use
    client.selected_channel = channel_id

@Client.on_callback_query(filters.regex(r'^index_cancel'))
async def index_cancel_callback(client, query):
    global CANCEL_INDEXING
    CANCEL_INDEXING = True
    await query.answer("🚫 Indexing cancelled", show_alert=True)

@Client.on_message(filters.private & filters.user(ADMIN_ID) & filters.text & ~filters.command(['index', 'start', 'help']))
async def handle_skip_input(client, message: Message):
    """Handle skip number or message link input after channel selection"""
    if not hasattr(client, 'selected_channel'):
        return
    
    channel_id = client.selected_channel
    user_input = message.text.strip()
    
    # Extract message ID from link or use as number
    skip = 0
    last_msg_id = None
    
    # Check if it's a message link
    if "t.me/" in user_input or "telegram.me/" in user_input:
        try:
            parts = user_input.split("/")
            msg_id = int(parts[-1])
            skip = msg_id
            await message.reply(f"**✅ Will start indexing from message ID: {msg_id}**")
        except:
            await message.reply("**❌ Invalid message link format!**")
            delattr(client, 'selected_channel')
            return
    else:
        # It's a number
        try:
            skip = int(user_input)
            if skip < 0:
                await message.reply("**❌ Skip number cannot be negative!**")
                delattr(client, 'selected_channel')
                return
            await message.reply(f"**✅ Will skip first {skip} messages**")
        except ValueError:
            await message.reply("**❌ Please send a valid number or message link!**")
            delattr(client, 'selected_channel')
            return
    
    # Get the last message ID from the channel
    try:
        chat = await client.get_chat(channel_id)
        # Try to get a recent message to determine the last message ID
        async for msg in client.get_chat_history(channel_id, limit=1):
            last_msg_id = msg.id
            break
        
        if not last_msg_id:
            await message.reply("**❌ Could not determine the last message ID in the channel.**")
            delattr(client, 'selected_channel')
            return
            
    except Exception as e:
        await message.reply(f"**❌ Error accessing channel: {str(e)}**")
        delattr(client, 'selected_channel')
        return
    
    # Confirm indexing
    buttons = [[
        InlineKeyboardButton('✅ Start Indexing', callback_data=f'start_index_{channel_id}_{last_msg_id}_{skip}')
    ], [
        InlineKeyboardButton('❌ Cancel', callback_data='index_cancel')
    ]]
    
    await message.reply(
        f"**📊 Indexing Summary:**\n\n"
        f"**Channel:** {chat.title}\n"
        f"**Last Message ID:** {last_msg_id}\n"
        f"**Skip:** {skip} messages\n"
        f"**Total to process:** ~{last_msg_id - skip} messages\n\n"
        f"**Do you want to start indexing?**",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    
    delattr(client, 'selected_channel')

@Client.on_callback_query(filters.regex(r'^start_index_'))
async def start_indexing(client, query):
    global CANCEL_INDEXING
    
    if query.from_user.id != ADMIN_ID:
        return await query.answer("⛔ You are not authorized!", show_alert=True)
    
    _, _, channel_id, last_msg_id, skip = query.data.split("_")
    channel_id = int(channel_id)
    last_msg_id = int(last_msg_id)
    skip = int(skip)
    
    await query.message.edit_text("**🔄 Starting indexing process...**")
    
    # Start indexing
    await index_files_to_db(client, query.message, channel_id, last_msg_id, skip)

async def index_files_to_db(client, msg: Message, channel_id: int, last_msg_id: int, skip: int):
    """Index files from channel to database"""
    global CANCEL_INDEXING
    CANCEL_INDEXING = False
    
    start_time = time.time()
    total_videos = 0
    duplicate = 0
    errors = 0
    deleted = 0
    no_media = 0
    current = skip
    
    async with indexing_lock:
        try:
            await msg.edit_text("**📥 Fetching messages from channel...**")
            
            async for message in client.get_chat_history(channel_id, offset_id=last_msg_id, offset=skip):
                if CANCEL_INDEXING:
                    time_taken = get_readable_time(time.time() - start_time)
                    await msg.edit_text(
                        f"**🚫 Indexing Cancelled!**\n\n"
                        f"⏱️ Time: {time_taken}\n"
                        f"✅ Saved: {total_videos}\n"
                        f"🔁 Duplicates: {duplicate}\n"
                        f"❌ Errors: {errors}\n"
                        f"🗑️ Deleted: {deleted}\n"
                        f"📝 No Media: {no_media}"
                    )
                    CANCEL_INDEXING = False
                    return
                
                current += 1
                
                # Update progress every 20 messages
                if current % 20 == 0:
                    time_taken = get_readable_time(time.time() - start_time)
                    btn = [[InlineKeyboardButton('🚫 Cancel', callback_data='index_cancel')]]
                    try:
                        await msg.edit_text(
                            f"**📊 Indexing Progress**\n\n"
                            f"⏱️ Time: {time_taken}\n"
                            f"📨 Processed: {current}\n"
                            f"✅ Saved: {total_videos}\n"
                            f"🔁 Duplicates: {duplicate}\n"
                            f"❌ Errors: {errors}\n"
                            f"🗑️ Deleted: {deleted}\n"
                            f"📝 No Media: {no_media}",
                            reply_markup=InlineKeyboardMarkup(btn)
                        )
                    except:
                        pass
                    await asyncio.sleep(1)
                
                # Check if message is deleted/empty
                if message.empty:
                    deleted += 1
                    continue
                
                # Check if message has video
                if not message.video:
                    no_media += 1
                    continue
                
                # Get video details
                video = message.video
                video_id = message.id
                duration = video.duration or 0
                
                # Save to database
                result = await mdb.save_video_id(video_id, channel_id, duration)
                
                if result == 'suc':
                    total_videos += 1
                elif result == 'dup':
                    duplicate += 1
                elif result == 'err':
                    errors += 1
                
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            await msg.reply(f'**❌ Indexing error: {str(e)}**')
        else:
            time_taken = get_readable_time(time.time() - start_time)
            await msg.edit_text(
                f"**✅ Indexing Complete!**\n\n"
                f"⏱️ Time: {time_taken}\n"
                f"📨 Total Processed: {current}\n"
                f"✅ Saved: {total_videos}\n"
                f"🔁 Duplicates: {duplicate}\n"
                f"❌ Errors: {errors}\n"
                f"🗑️ Deleted: {deleted}\n"
                f"📝 No Media: {no_media}"
            )
