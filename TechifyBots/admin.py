from pyrogram.types import *
from Database.userdb import udb
from Database.maindb import mdb
from vars import ADMIN_IDS, POST_CHANNEL
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


# ── /delete helpers ──────────────────────────────────────────────────────────

# Matches both private ("t.me/c/<internal_id>/<msg_id>") and public
# ("t.me/<username>/<msg_id>") Telegram message links, with or without a
# leading "https://" and with or without a trailing "?..." query string.
_MSG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(c/)?([A-Za-z0-9_]+)/(\d+)(?:[/?].*)?$", re.IGNORECASE
)


def _parse_msg_link(target: str):
    """
    Parse a Telegram message link into (channel_ref, msg_id).
    channel_ref is an int chat_id for private '/c/' links (already
    normalized with the -100 prefix), or a str username for public links.
    Returns None if `target` isn't a recognizable t.me message link.
    """
    m = _MSG_LINK_RE.search(target.strip())
    if not m:
        return None
    is_private, ident, mid = m.group(1), m.group(2), m.group(3)
    msg_id = int(mid)
    if is_private:
        if not ident.isdigit():
            return None
        return int(f"-100{ident}"), msg_id
    return ident, msg_id


async def _resolve_channel_id(client, channel_ref):
    """Resolve a channel_ref (int chat_id or str username) to a chat_id int."""
    if isinstance(channel_ref, int):
        return channel_ref
    try:
        chat = await client.get_chat(channel_ref)
        return chat.id
    except Exception:
        return None


async def _delete_one_target(client, target: str) -> str:
    """Delete a single /delete target (msg link, post_id, link_id, or video_id).
    Returns a one-line human-readable status string."""
    target = target.strip()
    if not target:
        return ""

    # ── 1) Telegram message link → delete that specific indexed file ────────
    parsed = _parse_msg_link(target)
    if parsed:
        channel_ref, msg_id = parsed
        chat_id = await _resolve_channel_id(client, channel_ref)
        if chat_id is None:
            return f"❌ `{target}` — couldn't resolve that channel (is the bot a member/admin there?)."
        result = await mdb.async_video_collection.delete_one(
            {"video_id": msg_id, "source_channel_id": chat_id}
        )
        if result.deleted_count:
            return f"✅ `{target}` — deleted indexed file (channel `{chat_id}`, msg `{msg_id}`)."
        return f"⚠️ `{target}` — no indexed DB entry found for channel `{chat_id}`, msg `{msg_id}`."

    # ── 2) Bare integer → legacy video_id ───────────────────────────────────
    try:
        video_id = int(target)
        deleted = await mdb.delete_video_by_id(video_id)
        if deleted:
            return f"✅ `{video_id}` — deleted video ID from videos DB."
    except ValueError:
        pass

    # ── 3) Post ID → delete all its files from DB + the actual channel post ─
    doc = await mdb.async_db["file_links"].find_one({"post_id": target})
    if doc:
        channel_msg_ids = doc.get("channel_msg_ids") or []
        post_channel_id = doc.get("post_channel_id", POST_CHANNEL)
        if channel_msg_ids:
            try:
                await client.delete_messages(post_channel_id, channel_msg_ids)
            except Exception as e:
                print(f"[/delete] failed to delete channel post(s) for post_id {target}: {e}")
        await mdb.async_db["file_links"].delete_one({"post_id": target})
        n_files = len(doc.get("files", []))
        if channel_msg_ids:
            return f"✅ `{target}` — deleted {n_files} file(s) from DB and removed the post from the channel."
        return f"✅ `{target}` — deleted {n_files} file(s) from DB (no stored channel message to remove — posted before this feature was added)."

    # ── 4) Link ID → same as post_id but keyed differently ──────────────────
    doc = await mdb.async_db["file_links"].find_one({"link_id": target})
    if doc:
        channel_msg_ids = doc.get("channel_msg_ids") or []
        post_channel_id = doc.get("post_channel_id", POST_CHANNEL)
        if channel_msg_ids:
            try:
                await client.delete_messages(post_channel_id, channel_msg_ids)
            except Exception as e:
                print(f"[/delete] failed to delete channel post(s) for link_id {target}: {e}")
        await mdb.async_db["file_links"].delete_one({"link_id": target})
        return f"✅ `{target}` — deleted link (and its channel post, if any)."

    return f"❌ `{target}` — no record found."


@Client.on_message(filters.command("delete") & filters.private)
async def delete_video_by_id_command(client, message):
    if not is_admin(message.from_user.id):
        return
    if len(message.command) < 2:
        await message.reply_text(
            "**Usage:**\n"
            "`/delete <msg_link>` — deletes that specific file from the indexed videos DB "
            "(the channel + message number are read straight from the link, so this works "
            "across any of your multiple index channels)\n"
            "`/delete <post_id>` — deletes all files under that Post ID from the DB **and** "
            "removes the post from the post channel\n"
            "`/delete <link_id>` — deletes an internal share/link record\n"
            "`/delete <video_id>` — legacy numeric video ID\n\n"
            "You can mix multiple targets in one command, comma-separated:\n"
            "`/delete https://t.me/c/1234567890/42, ABC123xyz09, 9187`"
        )
        return

    # Everything after the command name, so message links (which contain no
    # spaces but do need to be read as a whole) and comma-separated lists
    # both work correctly.
    raw = message.text.split(None, 1)[1] if len(message.text.split(None, 1)) > 1 else ""
    targets = [t.strip() for t in raw.split(",") if t.strip()]
    if not targets:
        await message.reply_text("No valid targets given.")
        return

    status = await message.reply_text(f"⏳ Processing {len(targets)} target(s)...")
    results = []
    for target in targets:
        try:
            results.append(await _delete_one_target(client, target))
        except Exception as e:
            results.append(f"❌ `{target}` — error: `{e}`")

    await status.edit_text("\n".join(results))


# ==================== /settings ====================
# Lets the admin flip core bot behavior from the bot DM (toggle buttons,
# backed by mdb.get_bot_settings()/set_bot_setting()) instead of editing
# vars.py / env vars and redeploying.

_SETTINGS_LABELS = {
    "is_verify":            "🔑 Verification System",
    "protect_content":      "🛡️ Protect Content",
    "premium_can_download": "📥 Premium Can Download",
    "is_fsub":               "📢 Force Subscribe",
    "premium_membership":   "💎 Premium Membership (category gate)",
}
# Rendered in this fixed order so the panel doesn't jump around on toggle.
_SETTINGS_ORDER = ["is_verify", "protect_content", "premium_can_download", "is_fsub", "premium_membership"]


def _settings_text(s: dict) -> str:
    lines = ["⚙️ <b>Bot Settings</b>\n", "Tap a toggle below to switch it ON/OFF.\n"]
    if not s["is_verify"]:
        lines.append("ℹ️ Verification is OFF → the free daily-limit system is also OFF (free users get unlimited files).")
    if not s["premium_membership"]:
        lines.append("ℹ️ Premium Membership is OFF → every user can switch category, not just Prime users.")
    if not s["protect_content"]:
        lines.append("ℹ️ Protect Content is OFF → files can be forwarded/saved by anyone.")
    return "\n".join(lines)


def _settings_markup(s: dict) -> InlineKeyboardMarkup:
    rows = []
    for key in _SETTINGS_ORDER:
        state_icon = "✅ ON" if s[key] else "❌ OFF"
        rows.append([InlineKeyboardButton(f"{_SETTINGS_LABELS[key]} : {state_icon}", callback_data=f"stg_toggle_{key}")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="close")])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.command("settings") & filters.private)
async def settings_command(client, message):
    if not is_admin(message.from_user.id):
        return
    s = await mdb.get_bot_settings()
    await message.reply_text(_settings_text(s), reply_markup=_settings_markup(s))


async def handle_settings_toggle(client, query):
    """Called from callback.py's dispatcher for callback_data starting with 'stg_toggle_'."""
    uid = query.from_user.id
    if not is_admin(uid):
        await query.answer("You are not my admin ❌", show_alert=True)
        return

    key = query.data[len("stg_toggle_"):]
    if key not in _SETTINGS_LABELS:
        await query.answer("Unknown setting.", show_alert=True)
        return

    current = await mdb.get_bot_settings()
    updated = await mdb.set_bot_setting(key, not current[key])
    await query.answer(f"{_SETTINGS_LABELS[key]} is now {'ON ✅' if updated[key] else 'OFF ❌'}")
    try:
        await query.message.edit_text(_settings_text(updated), reply_markup=_settings_markup(updated))
    except Exception:
        pass
