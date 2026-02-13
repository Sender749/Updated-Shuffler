from pyrogram import Client, filters
from vars import ADMIN_ID, ADMIN_USERNAME, IS_FSUB
from pyrogram.types import *
from Database.userdb import udb
from .fsub import get_fsub
from Database.maindb import mdb
import pytz
from datetime import datetime

@Client.on_message(filters.command("myplan") & filters.private)
async def my_plan(client, message):
    if await udb.is_user_banned(message.from_user.id):
        await message.reply("**🚫 You are banned from using this bot**",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support 🧑‍💻", url=f"https://t.me/{ADMIN_USERNAME}")]]))
        return
    if IS_FSUB and not await get_fsub(client, message):return
    
    global_limits = await mdb.get_global_limits()
    FREE_LIMIT = global_limits["free_limit"]
    user_id = message.from_user.id
    user = await mdb.get_user(user_id)
    plan = user.get("plan", "free")
    daily_count = user.get("daily_count", 0)
    prime_expiry = user.get("prime_expiry")
    status_text = f""">**Plan Details**

**User:** {message.from_user.mention}
**User ID:** {user_id}
**Plan:** {plan.capitalize()}
"""
    if plan == "free":
        status_text += f"""
**Daily Limit:** {FREE_LIMIT}
**Today Used:** {daily_count}/{FREE_LIMIT}
**Remaining:** {max(FREE_LIMIT - daily_count, 0)}
"""
    if plan == "prime" and prime_expiry:
        IST = pytz.timezone('Asia/Kolkata')
        if prime_expiry.tzinfo is None:
            prime_expiry = IST.localize(prime_expiry)
        status_text += f"""
🌟 **Unlimited Video Access**
⏳ **Expire Time:** {prime_expiry.strftime('%I:%M %p IST')}
📅 **Expire Date:** {prime_expiry.strftime('%d/%m/%Y')}
"""
    await message.reply_text(status_text)

@Client.on_message(filters.command("prime") & filters.private)
async def add_prime(client, message):
    if message.from_user.id != ADMIN_ID:
        await message.delete()
        await message.reply_text("**🚫 You're not authorized to use this command...**")
        return
    if len(message.command) != 3:
        await message.reply_text("**Usage: /prime {user_id} {duration}{unit}\n\nExample: /prime 123456789 2d\n\nUnits:\ns = seconds\nm = minutes\nh = hours\nd = days\ny = years**")
        return
    try:
        user_id = int(message.command[1])
        duration_input = message.command[2]
        import re
        match = re.match(r'(\d+)([smhdy])', duration_input)
        if not match:
            await message.reply_text("**⚠️ Invalid duration format. Use format like: 2d, 30m, 24h**")
            return
        amount, unit = match.groups()
        duration_str = f"{amount} {unit}"
        success = await mdb.add_prime(user_id, duration_str)
        if success:
            await message.reply_text(f"**✅ User {user_id} has been successfully added to the Prime Plan for {duration_str}.**")
            await client.send_message(chat_id=user_id, text="**🎉 Hey, You've been upgraded to a Premier user, check your plan by using /myplan**")
        else:
            await message.reply_text("**❌ Failed to add user to premium plan. Please check the user ID and duration format.**")
    except ValueError:
        await message.reply_text("**⚠️ Invalid user ID or duration format.**")
    except Exception as e:
        await message.reply_text(f"**❌ An error occurred: {str(e)}**")

@Client.on_message(filters.command("remove") & filters.private)
async def remove_prime(client, message):
    if message.from_user.id != ADMIN_ID:
        await message.delete()
        await message.reply_text("**🚫 You're not authorized to use this command...**")
        return
    if len(message.command) != 2:
        await message.reply_text("**Usage: /remove {user_id}**")
        return
    k = int(message.command[1])
    await mdb.remove_premium(k)
    await message.reply_text(f"**User {k} has been removed from the Prime Plan**")
    await client.send_message(chat_id=k, text="**Your premium access has been removed by the admin.**")

@Client.on_message(filters.command("setlimit") & filters.private)
async def set_limit(client, message):
    if message.from_user.id != ADMIN_ID:
        await message.delete()
        await message.reply_text("**🚫 You're not authorized to use this command...**")
        return
    if len(message.command) != 2:
        await message.reply_text("**Usage: /setlimit {new_free_limit}**")
        return
    try:
        new_value = int(message.command[1])
    except ValueError:
        await message.reply_text("**⚠️ Please provide a valid number for the new limit value**")
        return
    
    await mdb.update_global_limit("free", new_value)
    await message.reply_text(f"**✅ Free limit updated to {new_value} for all users**")

@Client.on_message(filters.command("resetlimits") & filters.private)
async def reset_limits(client, message):
    if message.from_user.id != ADMIN_ID:
        await message.delete()
        await message.reply_text("**🚫 You're not authorized to use this command...**")
        return
    
    count = await mdb.reset_all_free_limits()
    await message.reply_text(f"**✅ Daily limits have been reset for {count} free users**")

