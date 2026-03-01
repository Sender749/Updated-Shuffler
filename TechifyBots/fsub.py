import logging
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from vars import AUTH_CHANNELS
from pyrogram import Client
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate, PeerIdInvalid

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [FSUB] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


async def get_fsub(bot: Client, message, user_id: int = None) -> bool:
    if user_id is None:
        try:
            user_id = message.from_user.id
        except AttributeError:
            logger.warning("get_fsub: could not resolve user_id from message — allowing through")
            return True
    try:
        chat_id = message.chat.id
    except AttributeError:
        logger.warning(f"get_fsub: could not resolve chat_id for user {user_id} — allowing through")
        return True

    logger.info(f"get_fsub: checking user_id={user_id} in chat_id={chat_id}")
    if not AUTH_CHANNELS:
        logger.warning("get_fsub: AUTH_CHANNELS is EMPTY — fsub check skipped. "
                       "Set the AUTH_CHANNEL env variable!")
        return True

    logger.info(f"get_fsub: AUTH_CHANNELS to check: {AUTH_CHANNELS}")
    not_joined = []   # list of (title, invite_link)

    for channel_id in AUTH_CHANNELS:
        logger.info(f"get_fsub: checking channel {channel_id} for user {user_id}")
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            logger.info(f"get_fsub: user {user_id} status in {channel_id} = {member.status}")

        except UserNotParticipant:
            logger.info(f"get_fsub: user {user_id} has NOT joined channel {channel_id}")
            try:
                chat = await bot.get_chat(channel_id)
                invite_link = chat.invite_link
                if not invite_link:
                    logger.info(f"get_fsub: generating invite link for {channel_id}")
                    invite_link = await bot.export_chat_invite_link(channel_id)
                not_joined.append((chat.title, invite_link))
                logger.info(f"get_fsub: added '{chat.title}' to not-joined list")

            except ChatAdminRequired:
                logger.error(f"get_fsub: bot is NOT an admin in channel {channel_id} — "
                             "cannot fetch invite link. Add the bot as admin!")
            except ChannelPrivate:
                logger.error(f"get_fsub: channel {channel_id} is private and bot has no access")
            except PeerIdInvalid:
                logger.error(f"get_fsub: channel_id {channel_id} is INVALID — check AUTH_CHANNEL env var")
            except Exception as e:
                logger.error(f"get_fsub: unexpected error getting chat info for {channel_id}: {e}")

        except ChatAdminRequired:
            logger.error(f"get_fsub: bot is NOT an admin in {channel_id} — "
                         "cannot check membership. Add bot as admin!")
        except ChannelPrivate:
            logger.error(f"get_fsub: channel {channel_id} is private/inaccessible to bot")
        except PeerIdInvalid:
            logger.error(f"get_fsub: INVALID channel_id {channel_id} — check AUTH_CHANNEL env var")
        except Exception as e:
            logger.error(f"get_fsub: unexpected error checking membership in {channel_id}: {e}")
    if not not_joined:
        logger.info(f"get_fsub: user {user_id} has joined all channels — ALLOWED")
        return True
    logger.info(f"get_fsub: user {user_id} missing {len(not_joined)} channel(s) — sending fsub message")

    join_buttons = []
    for i in range(0, len(not_joined), 2):
        row = []
        for j in range(2):
            if i + j < len(not_joined):
                title, link = not_joined[i + j]
                row.append(InlineKeyboardButton(f"{i + j + 1}. {title}", url=link))
        join_buttons.append(row)
    try:
        tb = await bot.get_me()
        bot_username = tb.username
    except Exception as e:
        logger.error(f"get_fsub: could not get bot username: {e}")
        bot_username = "me"

    join_buttons.append([
        InlineKeyboardButton("🔄 Try Again", url=f"https://t.me/{bot_username}?start=start")
    ])
    try:
        mention = message.from_user.mention
    except AttributeError:
        mention = f"[User](tg://user?id={user_id})"

    fsub_text = (
        f"**🎭 {mention}, you haven't joined my required channel(s) yet.\n"
        f"Please join using the button(s) below, then tap 🔄 Try Again.**"
    )
    try:
        await bot.send_message(
            chat_id,
            fsub_text,
            reply_markup=InlineKeyboardMarkup(join_buttons)
        )
        logger.info(f"get_fsub: fsub message sent to chat_id={chat_id} for user {user_id}")
    except Exception as e:
        logger.error(f"get_fsub: FAILED to send fsub message to {chat_id}: {e}")

    return False
