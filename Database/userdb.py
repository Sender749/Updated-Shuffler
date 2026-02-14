from typing import Any
from vars import MONGO_URI
from motor import motor_asyncio
import pytz
IST = pytz.timezone("Asia/Kolkata")
client: motor_asyncio.AsyncIOMotorClient[Any] = motor_asyncio.AsyncIOMotorClient(MONGO_URI)
MT = client["Adultbot"]

class dypixx:
    def __init__(self):
        self.users = MT["users"]
        self.banned_users = MT["banned_users"]
        self.verify_users = MT["verify_users"]  # For verification system
        self.verify_id = MT["verify_id"]  # For verification tokens
        self.cache : dict[int, dict[str, Any]] = {}

    async def addUser(self, user_id: int, name: str) -> dict[str, Any] | None:
        try:
            user: dict[str, Any] = {"user_id": user_id, "name": name}
            await self.users.insert_one(user)
            self.cache[user_id] = user      
            return user
        except Exception as e:
            print("Error in addUser: ", e)
            

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        try:
            if user_id in self.cache:
                return self.cache[user_id]
            user = await self.users.find_one({"user_id": user_id})
            return user
        except Exception as e:
            print("Error in getUser: ", e)
            return None
    
    async def get_all_users(self) -> list[dict[str, Any]]:
        try:
            users : list[dict[str, Any]] = []
            async for user in self.users.find():
                users.append(user)
            return users
        except Exception as e:
            print("Error in getAllUsers: ", e)
            return []

    async def ban_user(self, user_id: int, reason: str = None) -> bool:
        try:
            ban_dypixx = {
                "user_id": user_id,
                "reason": reason
            }
            await self.banned_users.insert_one(ban_dypixx)
            return True
        except Exception as e:
            print("Error in banUser: ", e)
            return False

    async def unban_user(self, user_id: int) -> bool:
        try:
            result = await self.banned_users.delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except Exception as e:
            print("Error in unbanUser: ", e)
            return False

    async def is_user_banned(self, user_id: int) -> bool:
        try:
            user = await self.banned_users.find_one({"user_id": user_id})
            return user is not None
        except Exception as e:
            print("Error in isUserBanned: ", e)
            return False

    async def add_promo(self, button_text: str, reply_msg_id: int, promo_text: str, duration_seconds: int) -> dict[str, Any] | None:
        import time
        try:
            expire_at = int(time.time()) + duration_seconds
            promo = {
                "button_text": button_text,
                "reply_msg_id": reply_msg_id,
                "promo_text": promo_text,
                "expire_at": expire_at
            }
            await self.promos.insert_one(promo)
            return promo
        except Exception as e:
            print("Error in add_promo: ", e)
            return None

    async def get_active_promo(self) -> dict[str, Any] | None:
        import time
        try:
            now = int(time.time())
            promo = await self.promos.find_one({"expire_at": {"$gt": now}}, sort=[("expire_at", -1)])
            return promo
        except Exception as e:
            print("Error in get_active_promo: ", e)
            return None

    # ==================== VERIFICATION SYSTEM METHODS ====================
    
    async def get_verify_user(self, user_id: int) -> dict[str, Any] | None:
        """Get verification data for a user"""
        try:
            from datetime import datetime
            user = await self.verify_users.find_one({"user_id": user_id})
            if not user:
                # Create default verification record
                default_user = {
                    "user_id": user_id,
                    "last_verified": datetime(2020, 5, 17, 0, 0, 0, tzinfo=IST),
                    "second_time_verified": datetime(2019, 5, 17, 0, 0, 0, tzinfo=IST),
                    "third_time_verified": datetime(2018, 5, 17, 0, 0, 0, tzinfo=IST)
                }
                await self.verify_users.insert_one(default_user)
                return default_user
            return user
        except Exception as e:
            print("Error in get_verify_user: ", e)
            return None

    async def update_verify_user(self, user_id: int, value: dict) -> bool:
        """Update verification data for a user"""
        try:
            await self.verify_users.update_one(
                {"user_id": user_id}, 
                {"$set": value}, 
                upsert=True
            )
            return True
        except Exception as e:
            print("Error in update_verify_user: ", e)
            return False

    async def is_user_verified(self, user_id: int) -> bool:
        """Check if user's first verification is still valid"""
        try:
            from datetime import datetime
            user = await self.get_verify_user(user_id)
            if not user:
                return False
            
            past_date = user["last_verified"]
            if not isinstance(past_date, datetime):
                return False
            
            past_date = past_date.astimezone(IST)
            current_time = datetime.now(tz=IST)
            
            # Check if verified today
            seconds_since_midnight = (current_time - datetime(
                current_time.year, current_time.month, current_time.day, 
                0, 0, 0, tzinfo=IST
            )).total_seconds()
            
            time_diff = current_time - past_date
            total_seconds = time_diff.total_seconds()
            
            return total_seconds <= seconds_since_midnight
        except Exception as e:
            print("Error in is_user_verified: ", e)
            return False

    async def user_verified(self, user_id: int) -> bool:
        """Check if user's second verification is still valid"""
        try:
            from datetime import datetime
            user = await self.get_verify_user(user_id)
            if not user:
                return False
            
            past_date = user["second_time_verified"]
            if not isinstance(past_date, datetime):
                return False
            
            past_date = past_date.astimezone(IST)
            current_time = datetime.now(tz=IST)
            
            # Check if verified today
            seconds_since_midnight = (current_time - datetime(
                current_time.year, current_time.month, current_time.day, 
                0, 0, 0, tzinfo=IST
            )).total_seconds()
            
            time_diff = current_time - past_date
            total_seconds = time_diff.total_seconds()
            
            return total_seconds <= seconds_since_midnight
        except Exception as e:
            print("Error in user_verified: ", e)
            return False

    async def use_second_shortener(self, user_id: int, time: int) -> bool:
        """Check if second shortener should be used"""
        try:
            from datetime import datetime, timedelta
            user = await self.get_verify_user(user_id)
            if not user:
                return False
            
            if not user.get("second_time_verified"):
                await self.update_verify_user(user_id, {
                    "second_time_verified": datetime(2019, 5, 17, 0, 0, 0, tzinfo=IST)
                })
                user = await self.get_verify_user(user_id)
            
            if await self.is_user_verified(user_id):
                past_date = user["last_verified"].astimezone(IST)
                current_time = datetime.now(tz=IST)
                time_difference = current_time - past_date
                
                if time_difference.total_seconds() > time:
                    last_verified = user["last_verified"].astimezone(IST)
                    second_time = user["second_time_verified"].astimezone(IST)
                    return second_time < last_verified
            
            return False
        except Exception as e:
            print("Error in use_second_shortener: ", e)
            return False

    async def use_third_shortener(self, user_id: int, time: int) -> bool:
        """Check if third shortener should be used"""
        try:
            from datetime import datetime, timedelta
            user = await self.get_verify_user(user_id)
            if not user:
                return False
            
            if not user.get("third_time_verified"):
                await self.update_verify_user(user_id, {
                    "third_time_verified": datetime(2018, 5, 17, 0, 0, 0, tzinfo=IST)
                })
                user = await self.get_verify_user(user_id)
            
            if await self.user_verified(user_id):
                past_date = user["second_time_verified"].astimezone(IST)
                current_time = datetime.now(tz=IST)
                time_difference = current_time - past_date
                
                if time_difference.total_seconds() > time:
                    second_verified = user["second_time_verified"].astimezone(IST)
                    third_time = user["third_time_verified"].astimezone(IST)
                    return third_time < second_verified
            
            return False
        except Exception as e:
            print("Error in use_third_shortener: ", e)
            return False

    async def create_verify_id(self, user_id: int, hash: str) -> bool:
        """Create a verification ID for user"""
        try:
            res = {"user_id": user_id, "hash": hash, "verified": False}
            await self.verify_id.insert_one(res)
            return True
        except Exception as e:
            print("Error in create_verify_id: ", e)
            return False

    async def get_verify_id_info(self, user_id: int, hash: str) -> dict[str, Any] | None:
        """Get verification ID info"""
        try:
            return await self.verify_id.find_one({"user_id": user_id, "hash": hash})
        except Exception as e:
            print("Error in get_verify_id_info: ", e)
            return None

    async def update_verify_id_info(self, user_id: int, hash: str, value: dict) -> bool:
        """Update verification ID info"""
        try:
            await self.verify_id.update_one(
                {"user_id": user_id, "hash": hash},
                {"$set": value}
            )
            return True
        except Exception as e:
            print("Error in update_verify_id_info: ", e)
            return False

udb = dypixx()

