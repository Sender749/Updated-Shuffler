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

    async def check_and_increment_usage(self, user_id: int):
        limits, user = await asyncio.gather(self.get_global_limits(), self.get_user(user_id))
        free_limit = limits["free_limit"]
        plan = user.get("plan", "free")
        if plan == "prime":
            return {"allowed": True, "plan": "prime", "count": None, "limit": None}
        today = datetime.now()
        last_request = user.get("last_request_date")
        daily_count = user.get("daily_count", 0)
        if last_request and last_request.strftime("%Y-%m-%d") != today.strftime("%Y-%m-%d"):
            daily_count = 0
        if daily_count >= free_limit:
            return {"allowed": False, "plan": "free", "count": free_limit, "limit": free_limit}
        new_count = daily_count + 1
        await self.async_user_collection.update_one(
            {"_id": user_id},
            {"$set": {"daily_count": new_count, "last_request_date": today}}
        )
        return {"allowed": True, "plan": "free", "count": new_count, "limit": free_limit}

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
                {"$set": {"daily_count": 0, "last_request_date": datetime.now()}}
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

    # ── REELS WEBAPP / R2 USAGE ──────────────────────────────────────────────
    # We never call Cloudflare's own usage API (no extra credentials needed) —
    # since we're the only writer to the bucket, we just keep a running total
    # of bytes we've uploaded in a single-doc collection and alert off that.

    async def get_r2_usage(self) -> dict:
        doc = await self.async_r2_usage.find_one({"_id": "usage"})
        return {
            "total_bytes": (doc or {}).get("total_bytes", 0),
            "alert_sent": (doc or {}).get("alert_sent", False),
        }

    async def add_r2_usage_bytes(self, delta: int):
        """Adjust the tracked usage total (positive on upload, negative on delete)."""
        await self.async_r2_usage.update_one(
            {"_id": "usage"}, {"$inc": {"total_bytes": delta}}, upsert=True
        )

    async def set_r2_alert_sent(self, sent: bool):
        await self.async_r2_usage.update_one(
            {"_id": "usage"}, {"$set": {"alert_sent": sent}}, upsert=True
        )

    async def check_r2_usage_alert(self):
        """Background loop: DM all admins once tracked R2 usage crosses the
        configured free-tier alert threshold, so they can flip the WebApp off
        via /settings before anything would go beyond the free plan. Resets
        itself (so it can fire again) once usage drops back under threshold —
        e.g. after old reels are deleted."""
        from vars import R2_ENABLED, R2_FREE_STORAGE_GB, R2_ALERT_THRESHOLD_PERCENT, ADMIN_IDS
        if not R2_ENABLED:
            return
        limit_bytes = R2_FREE_STORAGE_GB * (1024 ** 3)
        for _ in count():
            try:
                usage = await self.get_r2_usage()
                percent = (usage["total_bytes"] / limit_bytes) * 100 if limit_bytes else 0
                if percent >= R2_ALERT_THRESHOLD_PERCENT and not usage["alert_sent"]:
                    used_gb = usage["total_bytes"] / (1024 ** 3)
                    msg = (
                        "⚠️ **Reels WebApp — R2 Storage Alert**\n\n"
                        f"Usage: **{used_gb:.2f} GB** / {R2_FREE_STORAGE_GB:.0f} GB free tier "
                        f"(**{percent:.1f}%**)\n\n"
                        "You're approaching Cloudflare R2's free storage limit. "
                        "Going over means paid usage starts.\n\n"
                        "Use /settings → 🎥 Reels WebApp to turn it OFF, or delete some "
                        "older reels to free up space."
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, msg)
                        except Exception as e:
                            print(f"[check_r2_usage_alert] failed to notify admin {admin_id}: {e}")
                    await self.set_r2_alert_sent(True)
                elif percent < R2_ALERT_THRESHOLD_PERCENT and usage["alert_sent"]:
                    await self.set_r2_alert_sent(False)
            except Exception as e:
                print(f"[check_r2_usage_alert] error: {e}")
            await asyncio.sleep(1800)  # check every 30 minutes

    # ── REELS FEED ────────────────────────────────────────────────────────────

    async def get_reels_feed(self, after_id: str = None, limit: int = 10) -> list:
        """
        Return up to `limit` reels-eligible videos (mirrored to R2), newest
        first, paginated with an ObjectId cursor (`after_id` = last _id the
        client already has).
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

    async def mark_reels_eligible(self, video_id: int, source_channel_id, r2_key: str, r2_size: int):
        await self.async_video_collection.update_one(
            {"video_id": video_id, "source_channel_id": source_channel_id},
            {"$set": {"reels_eligible": True, "r2_key": r2_key, "r2_size": r2_size}},
        )
        await self.add_r2_usage_bytes(r2_size)

    async def get_reels_eligible_pending(self, max_duration: int, batch_size: int = 25) -> list:
        """Existing indexed videos that qualify for reels but haven't been
        mirrored to R2 yet — used by the one-time /mirrorexisting backfill."""
        cursor = self.async_video_collection.find({
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
