"""Pacing tests: fake clock proves gap + batch cooldown behaviour."""

import asyncio
import unittest

from bot.utils import Pacer


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class PacingTests(unittest.TestCase):
    def test_gap_and_cooldown(self):
        calls = []

        async def fake(s):
            calls.append(s)

        async def go():
            p = Pacer(delay=3, batch=20, wait=30, sleep=fake)
            for _ in range(20):
                await p.tick()

        run(go())
        self.assertEqual(calls, [3] * 19 + [30])

    def test_cooldown_resets_counter(self):
        calls = []

        async def fake(s):
            calls.append(s)

        async def go():
            p = Pacer(delay=3, batch=20, wait=30, sleep=fake)
            for _ in range(21):
                await p.tick()

        run(go())
        self.assertEqual(calls, [3] * 19 + [30, 3])

    def test_configurable_values(self):
        calls = []

        async def fake(s):
            calls.append(s)

        async def go():
            p = Pacer(delay=1, batch=2, wait=5, sleep=fake)
            for _ in range(4):
                await p.tick()

        run(go())
        self.assertEqual(calls, [1, 5, 1, 5])

    def test_reconfigure_mid_run(self):
        calls = []

        async def fake(s):
            calls.append(s)

        async def go():
            p = Pacer(delay=3, batch=20, wait=30, sleep=fake)
            await p.tick()
            p.configure(delay=9, batch=2, wait=7)
            await p.tick()  # count=2 -> cooldown

        run(go())
        self.assertEqual(calls, [3, 7])


if __name__ == "__main__":
    unittest.main()
