from vars import *
import time
from pytz import timezone
from datetime import datetime
import os
from pyrogram import Client
from aiohttp import web

routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route(request):
    return web.Response(text="<h3 align='center'><b>I am Alive</b></h3>", content_type='text/html')

async def web_server():
    # Imported lazily (not at module top) because webapp_api -> Database.maindb
    # -> "from bot import bot", which would otherwise be a circular import at
    # module-load time. By the time web_server() actually runs (from
    # Bot.start(), after this module has fully finished loading), that import
    # resolves fine.
    from TechifyBots.webapp_api import register_webapp_routes
    register_webapp_routes(routes)
    app = web.Application(client_max_size=30_000_000)
    app.add_routes(routes)
    return app

class Bot(Client):
    def __init__(self):
        super().__init__(
            "techifybots",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=dict(root="TechifyBots"),
            workers=200,
            sleep_threshold=15
        )
        self.START_TIME = time.time()

    async def start(self):
        app = web.AppRunner(await web_server())
        await app.setup()
        try:
            await web.TCPSite(app, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
            print("Web server started.")
        except Exception as e:
            print(f"Web server error: {e}")

        await super().start()
        me = await self.get_me()
        print(f"Bot Started as {me.first_name}")

        # Notify all admins on start
        for admin_id in ADMIN_IDS:
            try:
                await self.send_message(admin_id, f"**{me.first_name} is started...**")
            except Exception as e:
                print(f"Error sending start message to admin {admin_id}: {e}")

        if LOG_CHNL:
            try:
                now = datetime.now(timezone("Asia/Kolkata"))
                msg = (
                    f"**{me.mention} is restarted!**\n\n"
                    f"📅 Date : `{now.strftime('%d %B, %Y')}`\n"
                    f"⏰ Time : `{now.strftime('%I:%M:%S %p')}`\n"
                    f"🌐 Timezone : `Asia/Kolkata`"
                )
                await self.send_message(LOG_CHNL, msg)
            except Exception as e:
                print(f"Error sending to LOG_CHANNEL: {e}")

    async def stop(self, *args):
        await super().stop()
        print("Bot stopped.")

bot = Bot()
