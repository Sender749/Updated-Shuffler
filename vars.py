import os
from typing import List

API_ID = int(os.getenv("API_ID", "25208597"))
API_HASH = os.getenv("API_HASH", "e99c3c5693d6d23a143b6ce760b7a6de")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://gd3251791_db_user:GDPQbmyXAEFDGpbL@cluster0.6jxsnxc.mongodb.net/?appName=Cluster0")
DATABASE_CHANNEL_ID = int(os.getenv("DATABASE_CHANNEL_ID", "-1002517753823"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "6541030917"))
PICS = (os.environ.get("PICS", "https://envs.sh/iKu.jpg https://envs.sh/iKE.jpg https://envs.sh/iKe.jpg https://envs.sh/iKi.jpg https://envs.sh/iKb.jpg")).split()
LOG_CHNL = int(os.getenv("LOG_CHNL", "-1002412135872"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Navex_69") # Without @
IS_FSUB = bool(os.environ.get("FSUB", False))
AUTH_CHANNELS = list(map(int, os.environ.get("AUTH_CHANNEL", "").split()))
DATABASE_CHANNEL_LOG = int(os.getenv("DATABASE_CHANNEL_LOG", "-1002412135872"))
DELETE_TIMER = int(os.getenv("DELETE_TIMER", "300"))  # seconds (default 5 minutes)
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "True").lower() == "true"
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
