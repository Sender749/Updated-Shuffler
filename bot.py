from vars import *
import time
from pytz import timezone
from datetime import datetime
import os
import asyncio
import aiohttp
from pyrogram import Client
from pyrogram.types import BotCommand
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


async def _keep_alive_loop():
    """
    Koyeb's free tier scales the whole instance to zero after a period of no
    incoming traffic — the next request then has to wait for a full cold
    boot (container start, Python/Pyrogram/Mongo init) before it can serve
    even the first byte of the WebApp's loading page. That's a big part of
    what reads as "stuck" when someone opens the WebApp after it's been
    idle: nothing can render, not even our own spinner, until the container
    wakes up.

    This pings the bot's own health-check route every few minutes so there's
    always been "recent traffic," which keeps Koyeb from scaling it down in
    the first place. Only runs if WEBAPP_URL is set, since that's the only
    thing this matters for.
    """
    if not WEBAPP_URL:
        return
    await asyncio.sleep(30)  # give the server a moment to finish starting up
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(WEBAPP_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    pass  # a response at all (any status) is what keeps it warm
            except Exception as e:
                print(f"[keep_alive] ping failed (harmless, will retry): {e}")
            await asyncio.sleep(600)  # every 10 minutes — comfortably under any free-tier idle timeout

class Bot(Client):
    def __init__(self):
        super().__init__(
            "techifybots",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=dict(root="TechifyBots"),
            # Pyrogram uses `workers` as a thread-pool size for dispatching
            # update handlers. On Koyeb's free tier (0.1 vCPU), only one
            # thread can ever actually execute Python bytecode at a time
            # regardless (the GIL) — so 200 threads bought zero real
            # parallelism and instead cost pure overhead: thread creation at
            # startup, per-thread memory, and OS scheduling/context-switch
            # churn competing for a CPU fraction that can barely service one
            # thread efficiently. 8 is comfortably enough concurrent-update
            # headroom for a small/medium bot without over-subscribing a
            # fractional CPU — this alone should measurably reduce general
            # command-response sluggishness.
            workers=8,
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

        asyncio.create_task(_keep_alive_loop())

        # Registers Telegram's native "/" command menu — shown to every user
        # when they type "/" in the chat, with a short description each.
        # Set fresh on every startup so it's always in sync with what the
        # bot actually supports, with no manual BotFather step needed.
        try:
            await self.set_bot_commands([
                BotCommand("start", "Check am i alive ?"),
                BotCommand("getvideos", "Get Spicy videos"),
                BotCommand("category", "Choose a video category"),
                BotCommand("myplan", "Check your daily limit and subscription"),
            ])
        except Exception as e:
            print(f"Error setting bot commands: {e}")

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
