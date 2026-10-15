"""Handler registration: real pyrofork Client objects, never started."""

import unittest
from types import SimpleNamespace

from pyrogram import Client

from bot import handlers
from bot.config import Config
from bot.db import DB


class HandlerRegistrationTests(unittest.TestCase):
    def test_register_with_session(self):
        bot = Client("t-bot", api_id=12345, api_hash="abc123",
                     bot_token="123:ABC", in_memory=True)
        user = Client("t-user", api_id=12345, api_hash="abc123",
                      in_memory=True)
        cfg = Config(api_id=12345, api_hash="abc123", bot_token="123:ABC",
                     owner_id=999, session_string="sess", port=8000,
                     db_path=":memory:")
        db = DB(":memory:")
        ctx = SimpleNamespace(cfg=cfg, db=db, eng_a=None, eng_b=None,
                              panel=None)
        handlers.register(bot, user, ctx)  # must not raise
        self.assertIsNotNone(ctx.panel)

    def test_register_without_session(self):
        # Source-B-only mode: user client is None
        bot = Client("t-bot2", api_id=12345, api_hash="abc123",
                     bot_token="123:ABC", in_memory=True)
        cfg = Config(api_id=12345, api_hash="abc123", bot_token="123:ABC",
                     owner_id=999, session_string="", port=8000,
                     db_path=":memory:")
        db = DB(":memory:")
        ctx = SimpleNamespace(cfg=cfg, db=db, eng_a=None, eng_b=None,
                              panel=None)
        handlers.register(bot, None, ctx)  # must not raise

    def test_handlers_actually_attached(self):
        import asyncio

        bot = Client("t-bot3", api_id=12345, api_hash="abc123",
                     bot_token="123:ABC", in_memory=True)
        cfg = Config(api_id=12345, api_hash="abc123", bot_token="123:ABC",
                     owner_id=999, session_string="", port=8000,
                     db_path=":memory:")
        ctx = SimpleNamespace(cfg=cfg, db=DB(":memory:"), eng_a=None,
                              eng_b=None, panel=None)
        handlers.register(bot, None, ctx)
        # add_handler() defers to a dispatcher task: pump its loop
        async def _wait():
            await asyncio.sleep(0.1)

        bot.dispatcher.loop.run_until_complete(_wait())
        # message + callback handlers were registered on the dispatcher
        groups = bot.dispatcher.groups
        names = [
            type(h).__name__
            for handlers_in_group in groups.values()
            for h in handlers_in_group
        ]
        n_msg = sum(1 for n in names if n == "MessageHandler")
        n_cb = sum(1 for n in names if n == "CallbackQueryHandler")
        self.assertTrue(n_msg > 10, f"only {n_msg} message handlers: {names}")
        self.assertTrue(n_cb > 0, "no callback handlers")


if __name__ == "__main__":
    unittest.main()
