from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from vars import ADMIN_ID, DELETE_TIMER, PROTECT_CONTENT
from Database.maindb import mdb
import string
import random
from datetime import datetime
import asyncio

# Store temporary link generation sessions
LINK_SESSIONS = {}

def generate_link_id():
    """Generate a unique 8-character link ID"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

# ==================== START LINK GENERATION ====================

@Client.on_message(filters.command("l") & filters.private & filters.user(ADMIN_ID))
async def start_link_generation(client: Client, message: Message):
    """Start the link generation process"""
    user_id = message.from_user.id
    
    # Initialize session
    LINK_SESSIONS[user_id] = {
        "files": [],
        "state": "collecting",
        "msg_id": None
    }
    
    sent = await message.reply_text(
        "**📁 Send files to generate link**\n\n"
        "Files collected: 0\n\n"
        "Use /m_link to generate the link"
    )
    
    LINK_SESSIONS[user_id]["msg_id"] = sent.id

# ==================== COLLECT FILES ====================

@Client.on_message(
    filters.private & 
    filters.user(ADMIN_ID) & 
    (filters.video | filters.photo | filters.document)
)
async def collect_files(client: Client, message: Message):
    """Collect files from admin for link generation"""
    user_id = message.from_user.id
    
    # Check if user has active link session
    if user_id not in LINK_SESSIONS or LINK_SESSIONS[user_id]["state"] != "collecting":
        return
    
    session = LINK_SESSIONS[user_id]
    
    # Extract file info
    file_info = {}
    if message.video:
        file_info = {
            "type": "video",
            "file_id": message.video.file_id,
            "duration": message.video.duration,
            "caption": message.caption or ""
        }
    elif message.photo:
        file_info = {
            "type": "photo",
            "file_id": message.photo.file_id,
            "caption": message.caption or ""
        }
    elif message.document:
        file_info = {
            "type": "document",
            "file_id": message.document.file_id,
            "file_name": message.document.file_name,
            "caption": message.caption or ""
        }
    
    # Add to session
    session["files"].append(file_info)
    
    # Update the collection message
    try:
        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=session["msg_id"],
            text=(
                "**📁 Send files to generate link**\n\n"
                f"Files collected: {len(session['files'])}\n\n"
                "Use /m_link to generate the link"
            )
        )
    except:
        pass

# ==================== GENERATE LINK ====================

@Client.on_message(filters.command("m_link") & filters.private & filters.user(ADMIN_ID))
async def generate_multi_link(client: Client, message: Message):
    """Generate link for collected files"""
    user_id = message.from_user.id
    
    # Check if user has active session
    if user_id not in LINK_SESSIONS:
        await message.reply_text("❌ No active link session. Use /l to start.")
        return
    
    session = LINK_SESSIONS[user_id]
    
    if not session["files"]:
        await message.reply_text("❌ No files collected. Send files first.")
        return
    
    # Generate unique link ID
    link_id = generate_link_id()
    
    # Store link data in database
    await mdb.async_db["file_links"].insert_one({
        "link_id": link_id,
        "files": session["files"],
        "created_by": user_id,
        "created_at": datetime.now(),
        "access_count": 0
    })
    
    # Get bot username
    bot_info = await client.get_me()
    link = f"https://t.me/{bot_info.username}?start=link_{link_id}"
    
    # Send link to admin
    await message.reply_text(
        f"✅ **Link Generated Successfully!**\n\n"
        f"🔗 Link: `{link}`\n\n"
        f"📁 Files: {len(session['files'])}\n"
        f"🆔 Link ID: `{link_id}`\n\n"
        f"Anyone can access these files through this link.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Copy Link", url=link)
        ]])
    )
    
    # Clear session
    del LINK_SESSIONS[user_id]

# ==================== HANDLE LINK ACCESS ====================

async def handle_link_access(client: Client, message: Message, link_id: str):
    """Handle when user accesses a generated link"""
    user_id = message.from_user.id
    
    # Get link data from database
    link_data = await mdb.async_db["file_links"].find_one({"link_id": link_id})
    
    if not link_data:
        await message.reply_text("❌ Invalid or expired link.")
        return
    
    files = link_data["files"]
    
    if not files:
        await message.reply_text("❌ No files found in this link.")
        return
    
    # Check user plan
    from .cmds import get_cached_user_data, get_cached_verification, show_verify, USER_ACTIVE_VIDEOS, auto_delete, USER_CURRENT_VIDEO
    from .fsub import get_fsub
    from vars import IS_FSUB, IS_VERIFY
    from Database.userdb import udb
    
    # Check if user is banned
    if await udb.is_user_banned(user_id):
        await message.reply_text("**🚫 You are banned**")
        return
    
    # Check force sub
    if IS_FSUB and not await get_fsub(client, message):
        return
    
    # Get user data
    user = await get_cached_user_data(user_id)
    is_prime = user.get("plan") == "prime"
    
    # Free users need to verify and check limits
    if not is_prime:
        if IS_VERIFY:
            verified, is_second, is_third = await get_cached_verification(user_id)
            
            if not verified or is_second or is_third:
                # Check daily limit
                usage = await mdb.check_and_increment_usage(user_id)
                if not usage["allowed"]:
                    await show_verify(client, message, user_id, is_second, is_third)
                    return
        else:
            # Just check limit
            usage = await mdb.check_and_increment_usage(user_id)
            if not usage["allowed"]:
                limits = await mdb.get_global_limits()
                await message.reply_text(
                    f"**🚫 Daily limit reached ({limits['free_limit']})**\n\n"
                    "Upgrade to Prime for unlimited access!"
                )
                return
    
    # Increment access count
    await mdb.async_db["file_links"].update_one(
        {"link_id": link_id},
        {"$inc": {"access_count": 1}}
    )
    
    # Send all files to user
    sent_messages = []
    
    # Determine usage text
    mins = DELETE_TIMER // 60
    if is_prime:
        usage_text = "🌟 Prime"
    else:
        usage_text = "📊 Link Access"
    
    for idx, file_info in enumerate(files):
        file_type = file_info["type"]
        file_id = file_info["file_id"]
        original_caption = file_info.get("caption", "")
        
        # For last file, add buttons and watch history
        if idx == len(files) - 1:
            # Build caption with delete timer
            full_caption = f"<b>⚠️ Delete: {mins}min\n\n{usage_text}</b>"
            if original_caption:
                full_caption += f"\n\n{original_caption}"
            
            # Store current video for this user
            USER_CURRENT_VIDEO[user_id] = file_id
            
            # Check watch history for back button
            history = await mdb.get_watch_history(user_id, limit=2)
            has_previous = len(history) > 0
            
            # Build buttons with short callback data
            buttons = []
            if has_previous:
                buttons.append([
                    InlineKeyboardButton("⬅️ Back", callback_data=f"prev_{user_id}"),
                    InlineKeyboardButton("🎬 Next", callback_data="getvideo")
                ])
            else:
                buttons.append([InlineKeyboardButton("🎬 Next", callback_data="getvideo")])
            
            buttons.append([InlineKeyboardButton("🔗 Share", callback_data=f"share_{user_id}")])
            
            try:
                if file_type == "video":
                    sent = await client.send_video(
                        message.chat.id,
                        file_id,
                        caption=full_caption,
                        protect_content=PROTECT_CONTENT,
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                elif file_type == "photo":
                    sent = await client.send_photo(
                        message.chat.id,
                        file_id,
                        caption=full_caption,
                        protect_content=PROTECT_CONTENT,
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                elif file_type == "document":
                    sent = await client.send_document(
                        message.chat.id,
                        file_id,
                        caption=full_caption,
                        protect_content=PROTECT_CONTENT,
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                
                # Add to watch history
                await mdb.add_to_watch_history(user_id, file_id, file_type)
                
                USER_ACTIVE_VIDEOS.setdefault(user_id, set()).add(sent.id)
                asyncio.create_task(auto_delete(client, message.chat.id, sent.id, user_id))
                sent_messages.append(sent.id)
            except Exception as e:
                print(f"Error sending last file: {e}")
        else:
            # Send files without buttons (not last file)
            try:
                if file_type == "video":
                    sent = await client.send_video(
                        message.chat.id,
                        file_id,
                        caption=original_caption,
                        protect_content=PROTECT_CONTENT
                    )
                elif file_type == "photo":
                    sent = await client.send_photo(
                        message.chat.id,
                        file_id,
                        caption=original_caption,
                        protect_content=PROTECT_CONTENT
                    )
                elif file_type == "document":
                    sent = await client.send_document(
                        message.chat.id,
                        file_id,
                        caption=original_caption,
                        protect_content=PROTECT_CONTENT
                    )
                
                sent_messages.append(sent.id)
            except Exception as e:
                print(f"Error sending file: {e}")
