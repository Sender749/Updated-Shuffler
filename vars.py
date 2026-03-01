import os
from typing import List

API_ID = int(os.getenv("API_ID", "25208597"))
API_HASH = os.getenv("API_HASH", "e99c3c5693d6d23a143b6ce760b7a6de")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://gd3251791_db_user:GDPQbmyXAEFDGpbL@cluster0.6jxsnxc.mongodb.net/?appName=Cluster0")

_channel_env = os.getenv("DATABASE_CHANNEL_ID", "-1002517753823")
if " " in _channel_env:
    DATABASE_CHANNEL_ID = [int(ch.strip()) for ch in _channel_env.split() if ch.strip().lstrip("-").isdigit()]
else:
    DATABASE_CHANNEL_ID = int(_channel_env)

ADMIN_ID = int(os.getenv("ADMIN_ID", "6541030917"))
PICS = (os.environ.get("PICS", "https://envs.sh/iKu.jpg https://envs.sh/iKE.jpg https://envs.sh/iKe.jpg https://envs.sh/iKi.jpg https://envs.sh/iKb.jpg")).split()
LOG_CHNL = int(os.getenv("LOG_CHNL", "-1002412135872"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Navex_69") # Without @
IS_FSUB = os.getenv("FSUB", "True").lower() == "true"
AUTH_CHANNELS = int(os.getenv("AUTH_CHANNELS", "-1002856477031").split())) if os.environ.get("AUTH_CHANNEL") else []
DELETE_TIMER = int(os.getenv("DELETE_TIMER", "300"))  # seconds (default 5 minutes)
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "True").lower() == "true"
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
POST_CHANNEL = int(os.getenv("POST_CHANNEL", "-1002856477031"))  # Channel ID where screenshots are posted (set 0 to disabel)

# Verification Settings
IS_VERIFY = os.getenv("IS_VERIFY", "True").lower() == "true"  # Enable/Disable verification
LOG_VR_CHANNEL = int(os.getenv("LOG_VR_CHANNEL", "-1002412135872"))  # Verification log channel
VERIFY_IMG = os.getenv("VERIFY_IMG", "https://graph.org/file/1669ab9af68eaa62c3ca4.jpg")  # Verification image

# Tutorial links for verification
TUTORIAL = os.getenv("TUTORIAL", "https://t.me/Navexdisscussion/33")
TUTORIAL2 = os.getenv("TUTORIAL2", "https://t.me/Navexdisscussion/33")
TUTORIAL3 = os.getenv("TUTORIAL3", "https://t.me/Navexdisscussion/33")

# Shortener settings for 3 verifications
SHORTENER_API = os.getenv("SHORTENER_API", "7ef9ed640db12a292b7c33f43922ded1feef2ddb")
SHORTENER_WEBSITE = os.getenv("SHORTENER_WEBSITE", "instantlinks.co")
SHORTENER_API2 = os.getenv("SHORTENER_API2", "bbe02c66b042f605c13ca910a0981014cf02e381")
SHORTENER_WEBSITE2 = os.getenv("SHORTENER_WEBSITE2", "instantlinks.co")
SHORTENER_API3 = os.getenv("SHORTENER_API3", "1423a167dd9dcfed061d49ca390a3c17aae34d24")
SHORTENER_WEBSITE3 = os.getenv("SHORTENER_WEBSITE3", "instantlinks.co")

# Verification expiry times (in seconds)
TWO_VERIFY_GAP = int(os.getenv("TWO_VERIFY_GAP", "21600"))  # in seconds
THREE_VERIFY_GAP = int(os.getenv("THREE_VERIFY_GAP", "21600"))  # in seconds
