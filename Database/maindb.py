from motor.motor_asyncio import AsyncIOMotorClient
from vars import *
from datetime import datetime, timedelta
import asyncio, time
from itertools import count
from bot import bot
from zoneinfo import ZoneInfo


class Database:
    def __init__(self):
        self.last_reset_time = datetime.now()
        self.cached_limits = None
        self.cached_limits_ts = 0
        self.cached_limits_ttl = 60
        self.cached_settings = None
        self.cached_settings_ts = 0
        self.cached_settings_ttl = 30
        self.async_client = AsyncIOMotorClient(
            MONGO_URI,
            maxPoolSize=20,
            waitQueueTimeoutMS=5000,
        )
        self.async_db = self.async_client["adultzonebot"]
        self.async_video_collection = self.async_db["videos"]
        self.async_user_collection = self.async_db["users"]
        self.async_limits_collection = self.async_db["limits"]
        self.async_global_limits = self.async_db["global_limits"]
        self.async_r2_usage = self.async_db["r2_usage"]
        self.async_reel_likes = self.async_db["reel_likes"]
        self.async_reel_views = self.async_db["reel_views"]
        self.async_mirror_channels = self.async_db["mirror_channels"]
        asyncio.create_task(self.check_and_reset_daily_counts())
        asyncio.create_task(self.check_premium_expire())
        asyncio.create_task(self.check_r2_usage_alert())

    # ── LIMITS ────────────────────────────────────────────────────────────────

    async def get_global_limits(self):
        now = time.monotonic()
        if self.cached_limits and (now - self.cached_limits_ts) < self.cached_limits_ttl:
            return self.cached_limits
        default_limits = {'free_limit': FREE_LIMIT, 'maintenance': False}
        db_limits = await self.async_global_limits.find_one({}) or {}
        self.cached_limits = {**default_limits, **db_limits}
        self.cached_limits_ts = now
        return self.cached_limits

    async def initialize_global_limits(self):
        if not await self.async_global_limits.find_one({}):
            await self.async_global_limits.insert_one({'free_limit': FREE_LIMIT, 'maintenance': False})

    # ── BOT SETTINGS (admin-configurable toggles, see /settings) ────────────────
    # These let the admin flip core behavior from the bot DM instead of editing
    # vars.py / env vars and redeploying. vars.py values are only used as the
    # initial defaults the very first time — after that the DB is authoritative.
    _SETTINGS_DEFAULTS_KEYS = (
        "is_verify", "protect_content", "premium_can_download",
        "is_fsub", "premium_membership", "webapp_enabled",
    )

    async def get_bot_settings(self) -> dict:
        now = time.monotonic()
        if self.cached_settings and (now - self.cached_settings_ts) < self.cached_settings_ttl:
            return self.cached_settings
        defaults = {
            "is_verify":             IS_VERIFY,
            "protect_content":       PROTECT_CONTENT,
            "premium_can_download":  PREMIUM_CAN_DOWNLOAD,
            "is_fsub":                IS_FSUB,
            # Whether category-switching requires a Prime plan.
            # True == old/original behavior (category switching is Premium-only).
            "premium_membership":    True,
            # Admin kill-switch for the Reels WebApp (see /settings). When False,
            # the WebApp shows users a "temporarily unavailable" popup instead of
            # the feed, without removing the button (see webapp_api.py).
            "webapp_enabled":        True,
        }
        db_settings = await self.async_db["bot_settings"].find_one({}) or {}
        self.cached_settings = {
            **defaults,
            **{k: v for k, v in db_settings.items() if k in self._SETTINGS_DEFAULTS_KEYS},
        }
        self.cached_settings_ts = now
        return self.cached_settings

    async def set_bot_setting(self, key: str, value: bool) -> dict:
        if key not in self._SETTINGS_DEFAULTS_KEYS:
            raise ValueError(f"Unknown setting: {key}")
        await self.async_db["bot_settings"].update_one({}, {"$set": {key: value}}, upsert=True)
        self.cached_settings_ts = 0  # force a fresh read next call
        return await self.get_bot_settings()

    async def _increment_daily_usage(self, user_id: int, count_field: str, date_field: str,
                                      limit: int, amount: int = 1) -> dict:
        """
        Shared day-rollover + increment logic — used by BOTH the DM flow
        (check_and_increment_usage) and the WebApp reels flow
        (check_and_increment_reels_usage). Literally the same function
        underneath, just pointed at different Mongo fields and limits, so
        each keeps its own separate quota (DM's FREE_LIMIT vs reels'
        REELS_FREE_LIMIT) while sharing identical reset-at-midnight and
        counting behavior — no more risk of the two drifting apart.

        Returns how much of `amount` could actually be served within
        today's remaining quota: `serve` may be less than `amount` (a
        partial batch right at the limit) or 0 (already exhausted). Prime
        users are always unlimited (limit/count come back as None).
        """
        user = await self.get_user(user_id)
        plan = (user or {}).get("plan", "free")
        if plan == "prime":
            return {"allowed": True, "serve": amount, "count": None, "limit": None}

        today = datetime.now()
        last_request = (user or {}).get(date_field)
        daily_count = (user or {}).get(count_field, 0)
        if last_request and last_request.strftime("%Y-%m-%d") != today.strftime("%Y-%m-%d"):
            daily_count = 0

        remaining = max(0, limit - daily_count)
        if remaining <= 0:
            return {"allowed": False, "serve": 0, "count": daily_count, "limit": limit}

        serve = min(amount, remaining)
        new_count = daily_count + serve
        await self.async_user_collection.update_one(
            {"_id": user_id},
            {"$set": {count_field: new_count, date_field: today}},
            upsert=True,
        )
        return {"allowed": True, "serve": serve, "count": new_count, "limit": limit}

    async def check_and_increment_usage(self, user_id: int):
        limits = await self.get_global_limits()
        result = await self._increment_daily_usage(
            user_id, "daily_count", "last_request_date", limits["free_limit"], amount=1
        )
        # Same shape existing DM callers (cmds.py, link_generator.py) expect.
        return {
            "allowed": result["allowed"],
            "plan": "prime" if result["limit"] is None else "free",
            "count": result["count"],
            "limit": result["limit"],
        }

    async def check_and_increment_reels_usage(self, user_id: int, requested: int) -> dict:
        """Same function as check_and_increment_usage above (see
        _increment_daily_usage) — just the WebApp reels feed's own separate
        counter/limit (REELS_FREE_LIMIT), since one feed request can hand
        out several videos at once."""
        from vars import REELS_FREE_LIMIT
        return await self._increment_daily_usage(
            user_id, "reels_daily_count", "reels_last_request_date", REELS_FREE_LIMIT, amount=requested
        )

    async def update_global_limit(self, limit_type, new_value):
        if limit_type == "free":
            await asyncio.gather(
                self.async_user_collection.update_many({"plan": "free"}, {"$set": {"daily_limit": new_value}}),
                self.async_limits_collection.update_one({"_id": "global_limits"}, {"$set": {"free_limit": new_value}}, upsert=True),
                self.async_global_limits.update_one({}, {"$set": {"free_limit": new_value}}, upsert=True),
            )
            self.cached_limits = None
        return True

    async def reset_all_free_limits(self):
        try:
            result = await self.async_user_collection.update_many(
                {"plan": "free"},
                {"$set": {
                    "daily_count": 0, "last_request_date": datetime.now(),
                    "reels_daily_count": 0, "reels_last_request_date": datetime.now(),
                }}
            )
            return result.modified_count
        except Exception as e:
            print(f"Error resetting free limits: {e}")
            return 0

    # ── MAINTENANCE ────────────────────────────────────────────────────────────

    async def set_maintenance_status(self, status: bool):
        await self.async_global_limits.update_one({}, {'$set': {'maintenance': status}}, upsert=True)
        self.cached_limits = None
        limits = await self.async_limits_collection.find_one({"_id": "global_limits"})
        if not limits:
            default_limits = {"_id": "global_limits", "free_limit": FREE_LIMIT}
            await self.async_limits_collection.insert_one(default_limits)
            return default_limits
        return limits

    # ── PREMIUM ────────────────────────────────────────────────────────────────

    async def check_and_reset_daily_counts(self):
        IST = ZoneInfo("Asia/Kolkata")
        for _ in count():
            try:
                now = datetime.now(IST)
                target_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
                if now >= target_time:
                    target_time += timedelta(days=1)
                sleep_seconds = (target_time - now).total_seconds()
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                await self.async_user_collection.update_many(
                    {"plan": "free"},
                    {"$set": {"daily_count": 0, "last_request_date": datetime.now(IST)}}
                )
                self.last_reset_time = datetime.now(IST)
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Error in daily count reset: {e}")
                await asyncio.sleep(60)

    async def check_premium_expire(self):
        for _ in count():
            try:
                now = datetime.now()
                expired = []
                for u in await self.get_all_premium_users():
                    expiry = u.get('prime_expiry')
                    if not expiry:
                        continue
                    # Strip timezone info for naive comparison
                    if hasattr(expiry, 'tzinfo') and expiry.tzinfo is not None:
                        from datetime import timezone
                        expiry = expiry.replace(tzinfo=None)
                    if expiry < now:
                        expired.append(u)
                if expired:
                    await asyncio.gather(*[self._expire_premium_user(u['_id']) for u in expired])
            except Exception as e:
                print(f"[check_premium_expire] error: {e}")
            await asyncio.sleep(60)

    async def _expire_premium_user(self, user_id: int):
        try:
            await self.remove_premium(user_id)
            # Reset category to "all" when premium expires
            await self.set_user_category(user_id, "all")
            await bot.send_message(
                user_id,
                "**⚠️ Your premium access has expired!**\n\n"
                "You have been moved back to the **Free Plan**.\n"
                "📂 Your category has been reset to **All** (default).\n\n"
                "Upgrade again to unlock category selection and other premium features!"
            )
        except Exception:
            pass

    async def get_all_premium_users(self):
        cursor = self.async_user_collection.find({"plan": 'prime'})
        return [doc async for doc in cursor]

    async def add_prime(self, user_id: int, duration_str: str):
        try:
            parts = duration_str.split()
            if len(parts) != 2 or parts[1] not in ("s", "m", "h", "d", "y"):
                return False
            amount = int(parts[0])
            unit = parts[1]
            if amount <= 0:
                return False
            now = datetime.now()
            expiry_date = now
            if unit == 's':   expiry_date += timedelta(seconds=amount)
            elif unit == 'm': expiry_date += timedelta(minutes=amount)
            elif unit == 'h': expiry_date += timedelta(hours=amount)
            elif unit == 'd': expiry_date += timedelta(days=amount)
            elif unit == 'y': expiry_date += timedelta(days=amount * 365)
            expiry_date = expiry_date.replace(second=0, microsecond=0)
            user = await self.get_user(user_id)
            if user.get('plan') == 'prime':
                await self.remove_premium(user_id)
            result = await self.async_user_collection.update_one(
                {"_id": user_id},
                {"$set": {"plan": "prime", "daily_limit": None, "daily_count": 0,
                          "prime_expiry": expiry_date, "last_request_date": now,
                          "remaining_time": format_remaining_time(expiry_date)}}
            )
            if result.modified_count > 0:
                updated_user = await self.get_user(user_id)
                return updated_user.get("plan") == "prime"
            return False
        except ValueError as e:
            print(f"Error in add_prime: {e}")
            return False

    async def remove_premium(self, user_id: int):
        limits = await self.get_global_limits()
        await self.async_user_collection.update_one(
            {"_id": user_id},
            {"$set": {"plan": "free", "daily_limit": limits["free_limit"], "daily_count": 0, "has_premium": False},
             "$unset": {"prime_expiry": "", "remaining_time": "", "premium_expire": ""}}
        )

    # ── VIDEOS ────────────────────────────────────────────────────────────────

    async def save_video_id(self, video_id: int, file_id: str, duration: int, is_premium: bool = False):
        video_data = {"video_id": video_id, "file_id": file_id, "duration": duration,
                      "is_premium": is_premium, "added_at": datetime.now()}
        if not await self.async_video_collection.find_one({"video_id": video_id}):
            await self.async_video_collection.insert_one(video_data)

    async def get_all_videos(self):
        return [v async for v in self.async_video_collection.find({})]

    async def count_all_videos(self):
        return await self.async_video_collection.count_documents({})

    async def get_user(self, user_id: int):
        user = await self.async_user_collection.find_one({"_id": user_id})
        if not user:
            limits = await self.get_global_limits()
            default_user = {"_id": user_id, "plan": "free", "daily_count": 0,
                            "daily_limit": limits["free_limit"], "last_request_date": datetime.now(),
                            "sent_videos": [], "prime_expiry": None, "remaining_time": None}
            await self.async_user_collection.insert_one(default_user)
            return default_user
        return user

    async def update_user(self, user_id: int, update_data: dict):
        await self.async_user_collection.update_one({"_id": user_id}, {"$set": update_data})

    async def get_sent_videos(self, user_id: int):
        user_data = await self.async_user_collection.find_one({"_id": user_id})
        return user_data.get("sent_videos", []) if user_data else []

    async def is_message_sent_to_user(self, user_id: int, message_id: int):
        user_data = await self.get_user(user_id)
        sent_videos = user_data.get("sent_videos", [])
        if not isinstance(sent_videos, list):
            sent_videos = []
        return any(entry.get("message_id") == message_id for entry in sent_videos if isinstance(entry, dict))

    async def remove_sent_video(self, user_id: int, video_id: int):
        await self.async_user_collection.update_one(
            {"_id": user_id}, {"$pull": {"sent_videos": {"video_id": video_id}}}
        )

    async def delete_all_videos(self):
        await self.async_video_collection.delete_many({})

    async def delete_video_by_id(self, video_id: int):
        await self.async_video_collection.delete_one({"video_id": video_id})
        return True

    # ── WATCH HISTORY ─────────────────────────────────────────────────────────

    async def add_to_watch_history(self, user_id: int, file_id: str, media_type: str):
        await self.async_db["watch_history"].update_one(
            {"user_id": user_id},
            {"$push": {"history": {
                "$each": [{"file_id": file_id, "media_type": media_type, "watched_at": datetime.now()}],
                "$position": 0, "$slice": 50
            }}},
            upsert=True
        )

    async def get_watch_history(self, user_id: int, limit: int = 50):
        doc = await self.async_db["watch_history"].find_one({"user_id": user_id})
        if doc and "history" in doc:
            return doc["history"][:limit]
        return []

    async def get_previous_video(self, user_id: int, current_file_id: str):
        history = await self.get_watch_history(user_id, limit=50)
        for idx, item in enumerate(history):
            if item["file_id"] == current_file_id and idx + 1 < len(history):
                return history[idx + 1]
        return None

    async def clear_watch_history_for_file(self, file_id: str):
        await self.async_db["watch_history"].update_many(
            {}, {"$pull": {"history": {"file_id": file_id}}}
        )

    # ── USER CATEGORY ─────────────────────────────────────────────────────────

    async def get_user_category(self, user_id: int) -> str:
        """Return the user's selected category name, or 'all' if not set."""
        doc = await self.async_user_collection.find_one({"_id": user_id}, {"category": 1})
        return (doc or {}).get("category", "all")

    async def set_user_category(self, user_id: int, category: str):
        """Persist the user's chosen category."""
        await self.async_user_collection.update_one(
            {"_id": user_id},
            {"$set": {"category": category}},
            upsert=True,
        )

    async def get_videos_by_channels(self, channel_ids: list):
        """Return videos whose source_channel_id is in channel_ids."""
        return [
            v async for v in self.async_video_collection.find(
                {"source_channel_id": {"$in": channel_ids}}
            )
        ]

    # ── REELS WEBAPP / R2 USAGE (per account) ────────────────────────────────
    # We never call Cloudflare's own usage API (no extra credentials needed) —
    # since we're the only writer to each bucket, we just keep a running total
    # of bytes uploaded per account (one doc per account_id) and alert off that.

    async def get_r2_usage(self, account_id: str) -> dict:
        doc = await self.async_r2_usage.find_one({"_id": account_id})
        return {
            "total_bytes": (doc or {}).get("total_bytes", 0),
            "alert_sent": (doc or {}).get("alert_sent", False),
        }

    async def add_r2_usage_bytes(self, account_id: str, delta: int):
        """Adjust one account's tracked usage total (positive on upload,
        negative on delete)."""
        await self.async_r2_usage.update_one(
            {"_id": account_id}, {"$inc": {"total_bytes": delta}}, upsert=True
        )

    async def set_r2_alert_sent(self, account_id: str, sent: bool):
        await self.async_r2_usage.update_one(
            {"_id": account_id}, {"$set": {"alert_sent": sent}}, upsert=True
        )

    async def check_r2_usage_alert(self):
        """Background loop: DMs admins in two situations —
        1. Any single configured account crosses its own free-tier alert
           threshold (so they can add another account, delete old reels, or
           flip the WebApp off before that one account incurs cost).
        2. EVERY configured account is full at once — at that point mirroring
           has actually stopped (see r2_uploader.pick_account_with_room), so
           this is a more urgent, separate alert.
        Each per-account alert resets itself once that account drops back
        under threshold; the all-full alert resets once any account regains
        room."""
        from vars import R2_ENABLED, R2_ACCOUNTS, R2_ALERT_THRESHOLD_PERCENT, ADMIN_IDS
        if not R2_ENABLED:
            return
        for _ in count():
            try:
                any_room = False
                for account in R2_ACCOUNTS:
                    usage = await self.get_r2_usage(account["id"])
                    limit_bytes = account["free_storage_gb"] * (1024 ** 3)
                    percent = (usage["total_bytes"] / limit_bytes) * 100 if limit_bytes else 0
                    if percent < 100:
                        any_room = True
                    if percent >= R2_ALERT_THRESHOLD_PERCENT and not usage["alert_sent"]:
                        used_gb = usage["total_bytes"] / (1024 ** 3)
                        msg = (
                            f"⚠️ **Reels WebApp — R2 Storage Alert ({account['id']})**\n\n"
                            f"Usage: **{used_gb:.2f} GB** / {account['free_storage_gb']:.0f} GB free tier "
                            f"(**{percent:.1f}%**)\n\n"
                            f"This account is approaching Cloudflare R2's free storage limit. "
                            f"The bot will automatically move on to the next configured account "
                            f"once this one fills up, but it's worth knowing which one is closest.\n\n"
                            f"Add another account block in r2_accounts.py (ACCOUNT {int(account['id'][3:]) + 1}), "
                            f"or delete some older "
                            f"reels mirrored under {account['id']} to free up space."
                        )
                        for admin_id in ADMIN_IDS:
                            try:
                                await bot.send_message(admin_id, msg)
                            except Exception as e:
                                print(f"[check_r2_usage_alert] failed to notify admin {admin_id}: {e}")
                        await self.set_r2_alert_sent(account["id"], True)
                    elif percent < R2_ALERT_THRESHOLD_PERCENT and usage["alert_sent"]:
                        await self.set_r2_alert_sent(account["id"], False)

                all_full_doc = await self.async_r2_usage.find_one({"_id": "_all_full_alert"})
                all_full_alert_sent = (all_full_doc or {}).get("sent", False)
                if not any_room and not all_full_alert_sent:
                    msg = (
                        "🛑 **Reels WebApp — All R2 accounts are full**\n\n"
                        f"All {len(R2_ACCOUNTS)} configured R2 account(s) have hit their free storage "
                        "limit. **Mirroring to R2 has stopped** — new videos will keep indexing "
                        "normally for DMs, but won't be added to the reels feed until there's room "
                        "again.\n\n"
                        "Add another account block in r2_accounts.py, or delete some "
                        "older reels to free up space on an existing account."
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, msg)
                        except Exception as e:
                            print(f"[check_r2_usage_alert] failed to notify admin {admin_id}: {e}")
                    await self.async_r2_usage.update_one(
                        {"_id": "_all_full_alert"}, {"$set": {"sent": True}}, upsert=True
                    )
                elif any_room and all_full_alert_sent:
                    await self.async_r2_usage.update_one(
                        {"_id": "_all_full_alert"}, {"$set": {"sent": False}}, upsert=True
                    )
            except Exception as e:
                print(f"[check_r2_usage_alert] error: {e}")
            await asyncio.sleep(1800)  # check every 30 minutes

    # ── MIRROR CHANNEL TOGGLES ────────────────────────────────────────────────
    # Per-channel on/off switch for AUTOMATIC mirroring (real-time indexing +
    # the bulk /index backfill). Manual /mirrorexisting always works regardless
    # of this toggle, since picking a channel there is already a deliberate,
    # explicit admin action. Any channel not yet explicitly toggled defaults
    # to ON, matching the original always-mirror-everything behavior.

    async def get_mirror_channel_state(self, channel_id) -> bool:
        doc = await self.async_mirror_channels.find_one({"_id": channel_id})
        return True if doc is None else doc.get("enabled", True)

    async def set_mirror_channel_state(self, channel_id, enabled: bool):
        await self.async_mirror_channels.update_one(
            {"_id": channel_id}, {"$set": {"enabled": enabled}}, upsert=True
        )

    async def get_all_mirror_channel_states(self, channel_ids: list) -> dict:
        cursor = self.async_mirror_channels.find({"_id": {"$in": channel_ids}})
        states = {doc["_id"]: doc.get("enabled", True) async for doc in cursor}
        for ch in channel_ids:
            states.setdefault(ch, True)
        return states

    # ── REELS FEED ────────────────────────────────────────────────────────────

    async def get_reels_feed(self, after_id: str = None, limit: int = 10) -> list:
        """
        Anonymous fallback (no verified Telegram user, so we have no way to
        remember what they've seen): plain newest-first pagination via an
        ObjectId cursor (`after_id` = last _id the client already has).
        """
        from bson import ObjectId
        query = {"reels_eligible": True}
        if after_id:
            try:
                query["_id"] = {"$lt": ObjectId(after_id)}
            except Exception:
                pass
        cursor = self.async_video_collection.find(query).sort("_id", -1).limit(limit)
        return [v async for v in cursor]

    async def get_fresh_reels_for_user(self, user_id: int, limit: int = 10) -> list:
        """
        For a verified Telegram user: return `limit` reels-eligible videos
        they HAVEN'T been shown before, in random order, and record them as
        seen — so re-opening the WebApp (or scrolling further) never repeats
        a video until every eligible video has been shown to them at least
        once. Once they've exhausted the pool, their seen-history resets
        automatically and the cycle starts over (a fresh shuffle again, not
        the same fixed order every time).
        """
        from bson import ObjectId

        seen_ids = [doc["video_id"] async for doc in self.async_reel_views.find(
            {"user_id": user_id}, {"video_id": 1}
        )]
        seen_object_ids = []
        for sid in seen_ids:
            try:
                seen_object_ids.append(ObjectId(sid))
            except Exception:
                pass

        pipeline = [
            {"$match": {"reels_eligible": True, "_id": {"$nin": seen_object_ids}}},
            {"$sample": {"size": limit}},
        ]
        docs = [d async for d in self.async_video_collection.aggregate(pipeline)]

        if not docs and seen_object_ids:
            # Seen everything eligible — reset their history and reshuffle
            # from the full pool instead of showing nothing.
            await self.async_reel_views.delete_many({"user_id": user_id})
            pipeline = [
                {"$match": {"reels_eligible": True}},
                {"$sample": {"size": limit}},
            ]
            docs = [d async for d in self.async_video_collection.aggregate(pipeline)]

        if docs:
            now = datetime.now()
            try:
                await self.async_reel_views.insert_many([
                    {"_id": f"{d['_id']}:{user_id}", "video_id": str(d["_id"]),
                     "user_id": user_id, "seen_at": now}
                    for d in docs
                ], ordered=False)
            except Exception:
                pass  # duplicate-key races (same video marked seen twice) are harmless — ignore

        return docs

    async def mark_reels_eligible(self, video_id: int, source_channel_id, r2_key: str,
                                   r2_size: int, r2_account: str, poster_key: str = None, poster_size: int = 0):
        update = {"reels_eligible": True, "r2_key": r2_key, "r2_size": r2_size, "r2_account": r2_account}
        if poster_key:
            update["poster_key"] = poster_key
        await self.async_video_collection.update_one(
            {"video_id": video_id, "source_channel_id": source_channel_id},
            {"$set": update},
        )
        await self.add_r2_usage_bytes(r2_account, r2_size + poster_size)

    # ── REELS LIKES ───────────────────────────────────────────────────────────
    # One doc per (video, user) in reel_likes, keyed so a user can only like a
    # given reel once. The running count itself lives on the video doc
    # (likes_count) so the feed can read it directly without a separate
    # aggregation on every request.

    async def toggle_like(self, video_object_id: str, user_id: int) -> dict:
        from bson import ObjectId
        like_key = f"{video_object_id}:{user_id}"
        existing = await self.async_reel_likes.find_one({"_id": like_key})
        if existing:
            await self.async_reel_likes.delete_one({"_id": like_key})
            delta = -1
            liked = False
        else:
            await self.async_reel_likes.insert_one({
                "_id": like_key, "video_id": video_object_id, "user_id": user_id,
                "liked_at": datetime.now(),
            })
            delta = 1
            liked = True

        result = await self.async_video_collection.find_one_and_update(
            {"_id": ObjectId(video_object_id)},
            {"$inc": {"likes_count": delta}},
            return_document=True,
        )
        count = max(0, (result or {}).get("likes_count", 0))
        return {"liked": liked, "count": count}

    async def get_liked_video_ids(self, user_id: int, video_object_ids: list) -> set:
        """Given a batch of video _ids (as strings) from a feed page, return
        the subset this user has already liked — used to mark hearts as
        filled/unfilled when the feed loads."""
        if not user_id or not video_object_ids:
            return set()
        keys = [f"{vid}:{user_id}" for vid in video_object_ids]
        cursor = self.async_reel_likes.find({"_id": {"$in": keys}}, {"video_id": 1})
        return {doc["video_id"] async for doc in cursor}

    async def get_reels_eligible_pending(self, max_duration: int, batch_size: int = 25) -> list:
        """Existing indexed videos that qualify for reels but haven't been
        mirrored to R2 yet — used by the one-time /mirrorexisting backfill.
        Excludes legacy docs indexed before multi-channel support (no valid
        source_channel_id) — those need /fix_index first, not this."""
        cursor = self.async_video_collection.find({
            "media_type": "video",
            "duration": {"$gt": 0, "$lte": max_duration},
            "reels_eligible": {"$ne": True},
            "source_channel_id": {"$exists": True, "$ne": None},
            "video_id": {"$exists": True, "$ne": None},
        }).limit(batch_size)
        return [v async for v in cursor]

    async def get_reels_eligible_range(self, source_channel_id, start_id: int, end_id: int,
                                        max_duration: int, batch_size: int = 25) -> list:
        """Same as get_reels_eligible_pending, but scoped to one channel and
        a [start_id, end_id] message-ID range — used by /mirrorexisting's
        channel+range picker flow."""
        cursor = self.async_video_collection.find({
            "source_channel_id": source_channel_id,
            "video_id": {"$gte": start_id, "$lte": end_id},
            "media_type": "video",
            "duration": {"$gt": 0, "$lte": max_duration},
            "reels_eligible": {"$ne": True},
        }).limit(batch_size)
        return [v async for v in cursor]


def format_remaining_time(expiry):
    delta = expiry - datetime.now()
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    seconds = delta.seconds % 60
    return f"{days}d {hours}h {minutes}m {seconds}s"


mdb = Database()
