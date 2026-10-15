"""Entry point: starts bot client, optional user client, engines, health server."""

import asyncio
import logging
import os
import signal
import sys
from types import SimpleNamespace

from aiohttp import web
from pyrogram import Client
from pyrogram.enums import ParseMode

from bot import handlers
from bot.config import load_config
from bot.db import DB
from bot.engine import CopyEngine
from bot.second import SourceBEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("copybot")


async def _health_server(port: int):
    async def ok(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health server on :%d", port)
    return runner


async def main():
    cfg = load_config()
    if not cfg.bot_token or not cfg.owner_id or not cfg.api_id or not cfg.api_hash:
        log.error("BOT_TOKEN, OWNER_ID, API_ID and API_HASH are required in .env")
        sys.exit(1)

    os.makedirs("data", exist_ok=True)
    db = DB(cfg.db_path)

    bot_client = Client(
        "copybot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        workdir="data",
        parse_mode=ParseMode.HTML,
    )

    user_client = None
    if cfg.has_session:
        user_client = Client(
            "usersession",
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            session_string=cfg.session_string,
            in_memory=True,
            no_updates=True,
        )
    else:
        log.info("SESSION_STRING not set — Source A disabled, Source B only.")

    async def notify_owner(html: str):
        try:
            await bot_client.send_message(cfg.owner_id, html, parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("could not DM owner", exc_info=True)

    eng_a = CopyEngine(db, user_client, notify=notify_owner, name="A")
    eng_b = SourceBEngine(db, bot_client, notify=notify_owner)
    ctx = SimpleNamespace(cfg=cfg, db=db, eng_a=eng_a, eng_b=eng_b, panel=None)
    handlers.register(bot_client, user_client, ctx)

    await bot_client.start()
    log.info("bot client started")
    if user_client:
        await user_client.start()
        log.info("user client started — warming peer cache")
        async for _ in user_client.get_dialogs():
            pass
        log.info("peer cache warm")

    runner = await _health_server(cfg.port)

    was_running = db.get("running")
    was_on2 = db.get("on2")
    try:
        if was_running or was_on2:
            await notify_owner("🔁 <b>Restarted — resuming copy.</b>")
        else:
            await notify_owner("✅ <b>Bot online.</b>")
    except Exception:
        pass

    if was_running and user_client:
        eng_a.start()
        log.info("auto-resumed Source A")
    elif was_running:
        db.set("running", False)
        log.info("running flag was set but no session — Source A left stopped")
    if was_on2:
        eng_b.start()
        log.info("auto-resumed Source B")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()

    # graceful shutdown (<=10s): stop engines, keep resume flags
    log.info("shutting down")
    try:
        await asyncio.wait_for(
            asyncio.gather(eng_a.stop(), eng_b.stop(), return_exceptions=True),
            timeout=9,
        )
    except asyncio.TimeoutError:
        log.warning("engine shutdown timed out")
    await runner.cleanup()
    try:
        if user_client:
            await user_client.stop()
        await bot_client.stop()
    except Exception:
        pass
    db.close()
    log.info("bye")


if __name__ == "__main__":
    asyncio.run(main())
