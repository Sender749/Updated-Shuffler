from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram import Client
from Script import text
from vars import ADMIN_ID
from Database.maindb import mdb
from .cmds import send_video_logic

@Client.on_callback_query()
async def callback_query_handler(client, query: CallbackQuery):
    try:
        if query.data == "start":
            await query.message.edit_caption(
                caption=text.START.format(query.from_user.mention),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Get Video", callback_data="getvideo")],
                    [InlineKeyboardButton("🍿 𝖡𝗎𝗒 𝖲𝗎𝖻𝗌𝖼𝗋𝗂𝗉𝗍𝗂𝗈𝗇 🍾", callback_data="pro")],
                    [InlineKeyboardButton("ℹ️ 𝖠𝖻𝗈𝗎𝗍", callback_data="about"),
                     InlineKeyboardButton("📚 𝖧𝖾𝗅𝗉", callback_data="help")]
                ])
            )

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
            pro_text = text.PRO.format(free_limit=current_limits['free_limit'], prime_limit=current_limits['prime_limit'])
            await query.message.edit_caption(
                caption=pro_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 𝖴𝗉𝗀𝗋𝖺𝖽𝖾 / 𝖯𝖺𝗒𝗆𝖾𝗇𝗍", user_id=int(ADMIN_ID))],
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
            await send_video_logic(client, query.message)
 

        elif query.data == "close":
            await query.message.delete()

    except Exception as e:
        print(f"Callback error: {e}")
        await query.answer("⚠️ An error occurred. Try again later.", show_alert=True)
