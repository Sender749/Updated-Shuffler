import logging
import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from vars import AUTH_CHANNELS
from pyrogram import Client
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate, PeerIdInvalid

# ─── Logger ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [FSUB] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ─── Bot info cache (permanent) ───────────────────────────────────────────────
_BOT_USERNAME_CACHE = None

# ─── FSUB membership cache: {user_id: {channel_id: (passed: bool, ts: float)}}
_FSUB_CACHE: dict = {}
_FSUB_CACHE_TTL = 120  # re-check membership every 2 minutes

# ─── Channel info cache (invite links rarely change) ─────────────────────────
_CHANNEL_INFO_CACHE: dict = {}  # {channel_id: (title, invite_link)}


async def _get_bot_username(bot: Client) -> str:
    global _BOT_USERNAME_CACHE
    if not _BOT_USERNAME_CACHE:
        me = await bot.get_me()
        _BOT_USERNAME_CACHE = me.username
    return _BOT_USERNAME_CACHE


async def _check_single_channel(bot: Client, user_id: int, channel_id: int) -> tuple:
    """
    Check membership for one channel.
    Returns (joined: bool, title: str | None, invite_link: str | None)
    """
    import time
    now = time.monotonic()

    # Fast path: membership cache
    user_cache = _FSUB_CACHE.get(user_id, {})
    cached = user_cache.get(channel_id)
    if cached:
        joined, ts = cached
        if now - ts < _FSUB_CACHE_TTL:
            if joined:
                return True, None, None

    try:
        await bot.get_chat_member(channel_id, user_id)
        _FSUB_CACHE.setdefault(user_id, {})[channel_id] = (True, now)
        return True, None, None

    except UserNotParticipant:
        _FSUB_CACHE.setdefault(user_id, {})[channel_id] = (False, now)
        # Get channel info (cached)
        if channel_id in _CHANNEL_INFO_CACHE:
            title, invite_link = _CHANNEL_INFO_CACHE[channel_id]
        else:
            try:
                chat = await bot.get_chat(channel_id)
                invite_link = chat.invite_link
                if not invite_link:
                    invite_link = await bot.export_chat_invite_link(channel_id)
                title = chat.title
                _CHANNEL_INFO_CACHE[channel_id] = (title, invite_link)
            except ChatAdminRequired:
                logger.error(f"get_fsub: bot is NOT admin in {channel_id}")
                return True, None, None
            except (ChannelPrivate, PeerIdInvalid) as e:
                logger.error(f"get_fsub: channel {channel_id} inaccessible: {e}")
                return True, None, None
            except Exception as e:
                logger.error(f"get_fsub: unexpected error getting chat {channel_id}: {e}")
                return True, None, None

        return False, title, invite_link

    except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid) as e:
        logger.error(f"get_fsub: error checking {channel_id}: {e}")
        return True, None, None
    except Exception as e:
        logger.error(f"get_fsub: unexpected error for channel {channel_id}: {e}")
        return True, None, None


async def get_fsub(bot: Client, message, user_id: int = None) -> bool:
    """
    Check if user has joined all forced subscription channels.
    All channel membership checks run in PARALLEL for maximum speed.
    Returns True if user passed fsub check, False otherwise.
    """

    if user_id is None:
        try:
            user_id = message.from_user.id
        except AttributeError:
            return True

    try:
        chat_id = message.chat.id
    except AttributeError:
        return True

    if not AUTH_CHANNELS:
        return True

    # Check ALL channels in PARALLEL
    results = await asyncio.gather(
        *[_check_single_channel(bot, user_id, ch) for ch in AUTH_CHANNELS],
        return_exceptions=False
    )

    not_joined = [
        (title, link)
        for joined, title, link in results
        if not joined and title and link
    ]

    if not not_joined:
        return True

    # Build join buttons
    join_buttons = []
    for i in range(0, len(not_joined), 2):
        row = []
        for j in range(2):
            if i + j < len(not_joined):
                title, link = not_joined[i + j]
                row.append(InlineKeyboardButton(f"{i + j + 1}. {title}", url=link))
        join_buttons.append(row)

    bot_username = await _get_bot_username(bot)
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
    except Exception as e:
        logger.error(f"get_fsub: FAILED to send fsub message to {chat_id}: {e}")

    return False


def invalidate_fsub_cache(user_id: int):
    """Call this after a user joins a channel to force a fresh check."""
    _FSUB_CACHE.pop(user_id, None)
