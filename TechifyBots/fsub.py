import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from vars import AUTH_CHANNELS
from pyrogram import Client
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate, PeerIdInvalid

# ── Bot username cache (permanent) ───────────────────────────────────────────
_BOT_USERNAME_CACHE = None

# ── Membership cache {user_id: {channel_id: (joined: bool, ts: float)}} ──────
_FSUB_CACHE: dict = {}
_FSUB_CACHE_TTL = 120  # seconds

# ── Channel info cache {channel_id: (title, invite_link, ts)} ────────────────
# BUGFIX: this used to cache the invite link forever (no timestamp/TTL) and
# it used to reuse chat.invite_link / export_chat_invite_link — i.e. the
# channel's single PRIMARY invite link. That primary link can be revoked or
# regenerated at any time from the Telegram app by any channel admin (or by
# another bot instance), which instantly invalidates it ("invite link
# expired/invalid" on the user's side) with no way for us to detect that
# client-side error. Since the old link was cached permanently, the bot kept
# handing out the same broken link to every new user until it was restarted.
#
# Fix: mint our OWN dedicated, non-expiring invite link with
# create_chat_invite_link (doesn't touch/replace the channel's primary link),
# and refresh it automatically every _CHANNEL_INFO_TTL seconds so any
# revoked/broken link self-heals on its own without needing a restart.
_CHANNEL_INFO_CACHE: dict = {}
_CHANNEL_INFO_TTL = 6 * 3600  # refresh invite links every 6 hours


async def _get_bot_username(bot: Client) -> str:
    global _BOT_USERNAME_CACHE
    if not _BOT_USERNAME_CACHE:
        me = await bot.get_me()
        _BOT_USERNAME_CACHE = me.username
    return _BOT_USERNAME_CACHE


async def _get_channel_invite(bot: Client, channel_id: int):
    """
    Return (title, invite_link) for a channel, generating/refreshing our own
    dedicated invite link automatically when there's none cached yet or the
    cached one has passed its TTL.
    """
    import time
    now = time.monotonic()
    cached = _CHANNEL_INFO_CACHE.get(channel_id)
    if cached:
        title, invite_link, ts = cached
        if now - ts < _CHANNEL_INFO_TTL:
            return title, invite_link

    try:
        chat = await bot.get_chat(channel_id)
        title = chat.title
        # Dedicated bot-owned invite link — never expires (no expire_date /
        # member_limit set) and doesn't disturb the channel's own primary
        # invite link the way export_chat_invite_link would.
        link_obj = await bot.create_chat_invite_link(channel_id, name="AutoSub")
        invite_link = link_obj.invite_link
        _CHANNEL_INFO_CACHE[channel_id] = (title, invite_link, now)
        return title, invite_link
    except Exception:
        # Generation failed (e.g. transient API error) — fall back to the
        # last known-good link rather than showing the user nothing, so a
        # temporary hiccup doesn't take fsub down entirely.
        if cached:
            title, invite_link, _ = cached
            return title, invite_link
        return None, None


async def _check_single_channel(bot: Client, user_id: int, channel_id: int) -> tuple:
    import time
    now = time.monotonic()
    user_cache = _FSUB_CACHE.get(user_id, {})
    cached = user_cache.get(channel_id)
    if cached:
        joined, ts = cached
        if now - ts < _FSUB_CACHE_TTL and joined:
            return True, None, None

    try:
        await bot.get_chat_member(channel_id, user_id)
        _FSUB_CACHE.setdefault(user_id, {})[channel_id] = (True, now)
        return True, None, None
    except UserNotParticipant:
        _FSUB_CACHE.setdefault(user_id, {})[channel_id] = (False, now)
        title, invite_link = await _get_channel_invite(bot, channel_id)
        if not invite_link:
            return True, None, None
        return False, title, invite_link
    except Exception:
        return True, None, None


async def get_fsub(bot: Client, message, user_id: int = None, start_param: str = None) -> bool:
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

    results = await asyncio.gather(
        *[_check_single_channel(bot, user_id, ch) for ch in AUTH_CHANNELS]
    )
    not_joined = [(t, l) for joined, t, l in results if not joined and t and l]
    if not not_joined:
        return True

    join_buttons = []
    for i in range(0, len(not_joined), 2):
        row = []
        for j in range(2):
            if i + j < len(not_joined):
                title, link = not_joined[i + j]
                row.append(InlineKeyboardButton(f"{i + j + 1}. {title}", url=link))
        join_buttons.append(row)

    bot_username = await _get_bot_username(bot)
    # BUGFIX: this used to always point at "?start=start", which threw away
    # the original deep-link payload (e.g. "share_<id>", "link_<id>").
    # After a user joined the required channel(s) and tapped "Try Again",
    # the bot would just show the generic /start screen instead of
    # delivering the file they originally clicked a share/generated link for.
    # Now we resume the exact same payload when one was given to us.
    resume_param = start_param or "start"
    join_buttons.append([InlineKeyboardButton("🔄 Try Again", url=f"https://t.me/{bot_username}?start={resume_param}")])

    try:
        mention = message.from_user.mention
    except AttributeError:
        mention = f"[User](tg://user?id={user_id})"

    try:
        await bot.send_message(
            chat_id,
            f"**🎭 {mention}, you haven't joined my required channel(s) yet.\n"
            f"Please join using the button(s) below, then tap 🔄 Try Again.**",
            reply_markup=InlineKeyboardMarkup(join_buttons)
        )
    except Exception:
        pass
    return False


def invalidate_fsub_cache(user_id: int):
    _FSUB_CACHE.pop(user_id, None)
