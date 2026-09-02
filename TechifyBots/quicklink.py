"""
Quick shortlink-chain generator.

Admin flow:
  1. /s                     → bot asks for a link
  2. admin sends the link   → bot shows all configured verification domains
                               (from vars.py) as toggleable buttons
  3. admin taps domains     → each tap adds/removes a ✅ tick and remembers
                               the order they were tapped in
  4. admin taps ➡️ Next      → bot builds a chain: domain[0]'s shortlink wraps
                               a bot deep-link that, once opened, hands out
                               domain[1]'s shortlink, and so on, with the
                               very last step handing out the original link.
                               Only the FIRST shortlink is given to the admin;
                               the rest are stored and released one at a time
                               as each step is completed.
"""
from __future__ import annotations
import random
import string
from datetime import datetime
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from vars import (
    ADMIN_IDS,
    SHORTENER_WEBSITE, SHORTENER_WEBSITE2, SHORTENER_WEBSITE3,
)
from Database.maindb import mdb
from .utils import get_shortlink

# ── admin check ───────────────────────────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _rand_id(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


# ── configured domains (index → display name), skipping unset ones ────────────

def _domains() -> list[tuple[int, str]]:
    raw = [
        (1, SHORTENER_WEBSITE),
        (2, SHORTENER_WEBSITE2),
        (3, SHORTENER_WEBSITE3),
    ]
    return [(idx, name) for idx, name in raw if name]


async def _shortlink_for(domain_idx: int, link: str) -> str:
    """Shorten `link` using the shortener that sits at `domain_idx` (1/2/3)."""
    return await get_shortlink(link, is_second=(domain_idx == 2), is_third=(domain_idx == 3))


QL_SESSIONS: dict[int, dict] = {}


def _new_sess(chat_id: int) -> dict:
    return {
        "chat_id":  chat_id,
        "state":    "await_link",
        "link":     None,
        "selected": [],       # ordered list of domain indices, in tap order
        "ask_msg_id": None,
    }


def _kill(uid: int):
    QL_SESSIONS.pop(uid, None)


# ── keyboards ───────────────────────────────────────────────────────────────

def _link_ask_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ql_cancel")]])


def _domain_kb(selected: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for idx, name in _domains():
        if idx in selected:
            order = selected.index(idx) + 1
            label = f"✅ {order}. {name}"
        else:
            label = f"{name}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ql_dm_{idx}")])
    rows.append([
        InlineKeyboardButton("➡️ Next", callback_data="ql_next"),
        InlineKeyboardButton("❌ Cancel", callback_data="ql_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _domain_text(link: str, selected: list[int]) -> str:
    if selected:
        names = [n for i, n in _domains() if i in selected]
        chosen = "\n".join(f"{k + 1}. {n}" for k, n in enumerate(names))
        sel_txt = f"\n\n**Selected (in order):**\n{chosen}"
    else:
        sel_txt = "\n\nTap the domains you want to use, in the order you want them applied."
    return (
        f"🔗 **Link:** `{link}`\n\n"
        f"Choose one or more verification domains to shorten this link with."
        f"{sel_txt}"
    )


# ── /s ────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("s") & filters.private)
async def cmd_s(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return
    uid = message.from_user.id

    old = QL_SESSIONS.get(uid)
    if old and old.get("ask_msg_id"):
        try:
            await client.delete_messages(old["chat_id"], old["ask_msg_id"])
        except Exception:
            pass

    if not _domains():
        await message.reply_text("⚠️ No shortener domains are configured in vars.py.")
        return

    s = _new_sess(message.chat.id)
    QL_SESSIONS[uid] = s
    ask = await message.reply_text(
        "🔗 **Send me the link you want to shorten.**",
        reply_markup=_link_ask_kb(),
    )
    s["ask_msg_id"] = ask.id


# ── receive the raw link ────────────────────────────────────────────────────

def _want_link(_, __, msg: Message) -> bool:
    if not msg.from_user or not msg.text:
        return False
    uid = msg.from_user.id
    s = QL_SESSIONS.get(uid)
    return bool(s and _is_admin(uid) and s["state"] == "await_link")


_link_filter = filters.create(_want_link)


@Client.on_message(_link_filter & filters.private)
async def receive_link(client: Client, message: Message):
    uid = message.from_user.id
    s = QL_SESSIONS.get(uid)
    if not s:
        return

    link = message.text.strip()
    if not (link.startswith("http://") or link.startswith("https://")):
        await message.reply_text("⚠️ That doesn't look like a link. Please send a valid http(s) URL.")
        return

    s["link"] = link
    s["state"] = "choosing"

    try:
        await client.delete_messages(message.chat.id, message.id)
    except Exception:
        pass
    if s.get("ask_msg_id"):
        try:
            await client.delete_messages(message.chat.id, s["ask_msg_id"])
        except Exception:
            pass

    ask = await client.send_message(
        message.chat.id,
        _domain_text(link, s["selected"]),
        reply_markup=_domain_kb(s["selected"]),
    )
    s["ask_msg_id"] = ask.id


# ── callback dispatcher (called by callback.py for "ql_" data) ───────────────

async def handle_ql_callback(client: Client, query, data: str):
    uid = query.from_user.id
    if not _is_admin(uid):
        await query.answer("❌ Not authorised.", show_alert=True)
        return

    if data == "ql_cancel":
        await query.answer("Cancelled.")
        try:
            await query.message.delete()
        except Exception:
            pass
        _kill(uid)
        return

    s = QL_SESSIONS.get(uid)
    if not s:
        await query.answer("No active session. Use /s to start.", show_alert=True)
        return

    if data.startswith("ql_dm_"):
        idx = int(data.split("_")[-1])
        if idx in s["selected"]:
            s["selected"].remove(idx)
        else:
            s["selected"].append(idx)
        await query.answer()
        try:
            await client.edit_message_text(
                s["chat_id"], s["ask_msg_id"],
                _domain_text(s["link"], s["selected"]),
                reply_markup=_domain_kb(s["selected"]),
            )
        except Exception:
            pass
        return

    if data == "ql_next":
        if not s["selected"]:
            await query.answer("Select at least one domain first.", show_alert=True)
            return
        await query.answer("Generating link…")
        await _finalize(client, uid)
        return


async def _finalize(client: Client, uid: int):
    s = QL_SESSIONS.get(uid)
    if not s:
        return

    chain = list(s["selected"])
    target = s["link"]
    qid = _rand_id(10)

    await mdb.async_db["quick_links"].insert_one({
        "qid":        qid,
        "chain":      chain,
        "target":     target,
        "created_by": uid,
        "created_at": datetime.now(),
    })

    bot_me = await client.get_me()
    first_deep_link = f"https://t.me/{bot_me.username}?start=ql_{qid}_0"
    first_short = await _shortlink_for(chain[0], first_deep_link)

    n = len(chain)
    steps_line = f"({n} step{'s' if n != 1 else ''} — {n - 1} more will be released one at a time)" if n > 1 else "(single step)"

    try:
        await client.delete_messages(s["chat_id"], s["ask_msg_id"])
    except Exception:
        pass

    await client.send_message(
        s["chat_id"],
        f"✅ **Shortlink ready!**\n\n{first_short}\n\n{steps_line}",
        disable_web_page_preview=True,
    )
    _kill(uid)


# ── handle_ql_start (called from cmds.py on ?start=ql_<qid>_<step>) ──────────

async def handle_ql_start(client: Client, message: Message, data: str):
    parts = data.split("_", 2)
    if len(parts) != 3:
        await message.reply_text("⚠️ Invalid or expired link.")
        return
    _, qid, step_s = parts
    try:
        step = int(step_s)
    except ValueError:
        await message.reply_text("⚠️ Invalid or expired link.")
        return

    rec = await mdb.async_db["quick_links"].find_one({"qid": qid})
    if not rec:
        await message.reply_text("⚠️ This link has expired or is invalid.")
        return

    chain = rec["chain"]
    target = rec["target"]

    if step < 0 or step >= len(chain):
        await message.reply_text("⚠️ Invalid or expired link.")
        return

    next_step = step + 1
    if next_step < len(chain):
        bot_me = await client.get_me()
        next_deep_link = f"https://t.me/{bot_me.username}?start=ql_{qid}_{next_step}"
        next_short = await _shortlink_for(chain[next_step], next_deep_link)
        await message.reply_text(
            f"✅ **Step {next_step}/{len(chain)} completed!**\n\n"
            f"Tap below to continue to the next step:\n\n{next_short}",
            disable_web_page_preview=True,
        )
    else:
        await message.reply_text(
            f"🎉 **All steps completed!**\n\nHere's your link:\n\n{target}",
            disable_web_page_preview=True,
        )
