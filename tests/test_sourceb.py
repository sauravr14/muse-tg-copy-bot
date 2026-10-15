"""Source B tests: queue drain order, live enqueue, off-switch, restart survival."""

import asyncio
import unittest

from bot.second import SourceBEngine

from .fakes import FakeClient, instant_sleep_factory, make_db, msg


def new_loop_run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def drain(eng, secs=0.3):
    """Run the engine briefly, then stop it."""
    task = asyncio.ensure_future(eng._run())
    await asyncio.sleep(secs)
    eng.stop_event.set()
    await asyncio.gather(task, return_exceptions=True)


class SourceBTests(unittest.TestCase):
    def _setup(self, ids, **dbkw):
        log = []
        messages = [msg(i, log=log) for i in ids]
        db = make_db(**dbkw)
        db.set("src2", 111)
        db.set("on2", True)
        for i in ids:
            db.q2_add(111, i)
        eng = SourceBEngine(db, FakeClient(messages),
                            sleeper=instant_sleep_factory([]))
        return db, eng, log

    def test_queue_drains_ascending(self):
        db, eng, log = self._setup([5, 3, 1, 4, 2])

        async def go():
            await drain(eng)

        new_loop_run(go())
        self.assertEqual([c["id"] for c in log], [1, 2, 3, 4, 5])
        self.assertEqual(db.q2_count(111), 0)

    def test_missing_ids_dropped(self):
        # id 99 queued but Telegram returns nothing for it -> dropped, no loop
        db, eng, log = self._setup([1, 2])
        db.q2_add(111, 99)

        async def go():
            await drain(eng)

        new_loop_run(go())
        self.assertEqual([c["id"] for c in log], [1, 2])
        self.assertEqual(db.q2_count(111), 0)

    def test_live_enqueue(self):
        db, eng, log = self._setup([])
        self.assertTrue(eng.enqueue(7))
        self.assertEqual(db.q2_next(111, 10), [7])
        # duplicate enqueue is ignored
        self.assertFalse(eng.enqueue(7))

    def test_off_stops_processing(self):
        db, eng, log = self._setup([1, 2, 3])
        db.set("on2", False)

        async def go():
            await drain(eng)

        new_loop_run(go())
        self.assertEqual(log, [])
        self.assertEqual(db.q2_count(111), 3)  # queue kept

    def test_queue_survives_restart(self):
        db, eng, log = self._setup([1, 2, 3])

        async def partial():
            # stop the engine right after its first pacing tick -> 1 copied
            async def stop_after_first(secs):
                eng.stop_event.set()
                await asyncio.sleep(0)

            eng._pacer.sleep = stop_after_first
            await eng._run()

        new_loop_run(partial())
        remaining = db.q2_next(111, 10)
        self.assertEqual(len(log), 1)
        self.assertEqual(remaining, [2, 3])

        # new engine instance on the same DB resumes the rest
        log2 = []
        messages = [msg(i, log=log2) for i in remaining]
        eng2 = SourceBEngine(db, FakeClient(messages),
                             sleeper=instant_sleep_factory([]))

        async def go():
            await drain(eng2)

        new_loop_run(go())
        self.assertEqual(sorted(c["id"] for c in log2), sorted(remaining))
        self.assertEqual(db.q2_count(111), 0)

    def test_backfill_range(self):
        db = make_db()
        db.set("src2", 111)
        eng = SourceBEngine(db, FakeClient([]),
                            sleeper=instant_sleep_factory([]))
        n = eng.backfill(10, 15)
        self.assertEqual(n, 6)
        self.assertEqual(db.q2_next(111, 10), [10, 11, 12, 13, 14, 15])
        with self.assertRaises(ValueError):
            eng.backfill(1, 3_000_000)


if __name__ == "__main__":
    unittest.main()
