"""Engine tests: dedupe skip, resume-after-restart sends nothing."""

import asyncio
import unittest

from bot.engine import CopyEngine

from .fakes import FakeClient, instant_sleep_factory, make_db, msg


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class EngineTests(unittest.TestCase):
    def test_dedupe_skips_repeats(self):
        log = []
        calls = []
        messages = [
            msg(1, fuid="same", log=log),
            msg(2, fuid="same", log=log),   # duplicate file -> skipped
            msg(3, fuid="other", log=log),
            FakeMessageText(4),              # text-only -> skipped
        ]
        db = make_db()
        eng = CopyEngine(db, FakeClient(messages),
                         sleeper=instant_sleep_factory(calls))

        async def go():
            await eng.run()

        run(go())
        copied_ids = [c["id"] for c in log]
        self.assertEqual(copied_ids, [1, 3])
        self.assertEqual(eng.skipped, 2)
        self.assertEqual(db.progress_get(111), 4)

    def test_resume_after_restart_sends_nothing(self):
        log = []
        calls = []
        messages = [msg(i, log=log) for i in range(1, 6)]
        db = make_db()
        client = FakeClient(messages)

        async def first():
            eng = CopyEngine(db, client, sleeper=instant_sleep_factory(calls))
            await eng.run()

        run(first())
        self.assertEqual(len(log), 5)

        # "restart": brand-new engine instance on the same DB
        async def second():
            eng2 = CopyEngine(db, client, sleeper=instant_sleep_factory([]))
            await eng2.run()
            return eng2

        eng2 = run(second())
        self.assertEqual(len(log), 5)  # nothing new copied
        self.assertEqual(eng2.sent, 0)

    def test_type_filter(self):
        log = []
        messages = [
            msg(1, media_type="document", log=log),
            msg(2, media_type="sticker", log=log),  # not in default types
        ]
        db = make_db()
        eng = CopyEngine(db, FakeClient(messages),
                         sleeper=instant_sleep_factory([]))

        async def go():
            await eng.run()

        run(go())
        self.assertEqual([c["id"] for c in log], [1])
        self.assertEqual(eng.skipped, 1)

    def test_no_session_refuses_gracefully(self):
        db = make_db()
        eng = CopyEngine(db, None, sleeper=instant_sleep_factory([]))
        notes = []

        async def collect(h):
            notes.append(h)

        eng.notify = collect

        async def go():
            await eng.run()

        asyncio.new_event_loop().run_until_complete(go())
        self.assertFalse(db.get("running"))
        self.assertTrue(any("SESSION_STRING" in n for n in notes))


class FakeMessageText:
    """Text-only message: no media attrs set."""

    def __init__(self, id):
        self.id = id
        self.empty = False
        for attr in ("document", "video", "audio", "photo",
                     "animation", "voice", "video_note", "sticker"):
            setattr(self, attr, None)
        self.caption = None


if __name__ == "__main__":
    unittest.main()
