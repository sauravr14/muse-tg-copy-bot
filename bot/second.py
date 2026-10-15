"""Source B engine: the bot itself reads+copies from a channel where it is admin.

Queue-based (SQLite q2 table, ascending id order). Fully independent from
Source A — own loop, own pacing counters (shared delay/batch/wait values),
own stats. No pyrofork imports (duck-typed errors) for testability.
"""

import asyncio
import logging
import time

from .engine import FATAL_ERRORS, FatalCopyError, _err_code, _flood_secs
from .utils import (
    Pacer,
    StopRequested,
    apply_caption,
    caption_html_of,
    file_info,
    interruptible_sleep,
    media_of,
)

log = logging.getLogger("copybot.second")

_FLOOD_NAMES = ("FloodWait", "SlowmodeWait")
_CHUNK = 100
_NET_RETRY = 5


class SourceBEngine:
    def __init__(self, db, bot_client, notify=None, sleeper=None):
        self.db = db
        self.client = bot_client
        self.notify = notify
        self.stop_event = asyncio.Event()
        self._sleeper = sleeper
        self._task = None
        self._pacer = Pacer(sleep=self._make_sleep())
        self._reset_stats()

    def _make_sleep(self):
        if self._sleeper is not None:
            return self._sleeper
        return interruptible_sleep(self.stop_event)

    @property
    def _sleep(self):
        return self._pacer.sleep

    def _reset_stats(self):
        self.sent = 0
        self.skipped = 0
        self.failed = 0
        self.started_at = time.time()
        self.state = "idle"
        self._pacer.count = 0

    # -- lifecycle ------------------------------------------------------
    def start(self) -> bool:
        if self._task and not self._task.done():
            return False
        self.stop_event.clear()
        self._pacer.sleep = self._make_sleep()
        self._task = asyncio.create_task(self._run(), name="copy-engine-B")
        return True

    async def stop(self):
        self.stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=9)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _notify(self, html: str):
        if self.notify:
            try:
                await self.notify(html)
            except Exception:
                log.warning("notify failed", exc_info=True)

    # -- queue ----------------------------------------------------------
    def enqueue(self, msg_id) -> bool:
        src = self.db.get("src2")
        if not src:
            return False
        return self.db.q2_add(src, msg_id)

    def backfill(self, start, end) -> int:
        src = self.db.get("src2")
        if not src:
            return 0
        if end - start > 2_000_000:
            raise ValueError("range too big (max 2,000,000 ids)")
        return self.db.q2_add_range(src, start, end)

    # -- guarded API ----------------------------------------------------
    async def _guard(self, coro):
        floods = 0
        while True:
            try:
                return await coro
            except StopRequested:
                raise
            except Exception as e:
                name = type(e).__name__
                if name in _FLOOD_NAMES:
                    floods += 1
                    if floods > 8:
                        raise FatalCopyError(f"repeated {name}")
                    await self._sleep(_flood_secs(e))
                    continue
                if _err_code(e) in FATAL_ERRORS:
                    raise FatalCopyError(_err_code(e))
                if isinstance(e, (OSError, asyncio.TimeoutError)):
                    await self._sleep(_NET_RETRY)
                    continue
                log.warning("Source B API soft error: %r", e)
                return None

    async def _copy_msg(self, msg, dest, caption) -> bool:
        cap = caption
        fixed = False
        floods = 0
        while True:
            try:
                await msg.copy(dest, caption=cap or None, parse_mode="HTML")
                return True
            except StopRequested:
                raise
            except Exception as e:
                name = type(e).__name__
                if name in _FLOOD_NAMES:
                    floods += 1
                    if floods > 8:
                        raise FatalCopyError(f"repeated {name}")
                    await self._sleep(_flood_secs(e))
                    continue
                code = _err_code(e)
                if code == "CAPTION_TOO_LONG" and not fixed:
                    fixed = True
                    import re as _re

                    cap = _re.sub(r"<[^>]+>", "", cap or "")[:1024]
                    continue
                if code in FATAL_ERRORS:
                    raise FatalCopyError(code)
                if isinstance(e, (OSError, asyncio.TimeoutError)):
                    await self._sleep(_NET_RETRY)
                    continue
                log.warning("Source B copy of msg %s failed (%s)",
                            getattr(msg, "id", "?"), code)
                return False

    # -- main loop ------------------------------------------------------
    async def _run(self):
        db = self.db
        # protected-content check at start
        src0 = db.get("src2")
        if src0 and self.client is not None:
            try:
                chat = await self._guard(self.client.get_chat(src0))
            except (FatalCopyError, StopRequested):
                db.set("on2", False)
                raise
            if chat is not None and getattr(chat, "has_protected_content", False):
                await self._notify(
                    "Source B has “Restrict saving content” enabled — refused."
                )
                db.set("on2", False)

        self._reset_stats()
        try:
            while not self.stop_event.is_set():
                if not db.get("on2"):
                    self.state = "idle"
                    await self._sleep(1)
                    continue
                src = db.get("src2")
                dest = db.get("dest2") or db.get("dest")
                if not src or not dest or str(src) == str(dest):
                    self.state = "idle"
                    await self._sleep(2)
                    continue

                ids = db.q2_next(src, _CHUNK)
                if not ids:
                    self.state = "watching"
                    await self._sleep(2)
                    continue

                self.state = "copying"
                msgs = await self._guard(self.client.get_messages(src, ids))
                if msgs is None:
                    await self._sleep(5)
                    continue
                got = {
                    m.id
                    for m in msgs
                    if m is not None and not getattr(m, "empty", False)
                }
                # ids Telegram returns nothing for are dropped so they never loop
                for mid in ids:
                    if mid not in got:
                        db.q2_drop(src, mid)

                cap_cfg = {
                    "nocaption": db.get("nocaption"),
                    "remove_links": db.get("remove_links"),
                    "remove_usernames": db.get("remove_usernames"),
                    "blocklist": db.get("blocklist"),
                    "replacements": db.get("replacements"),
                    "template": db.get("template"),
                    "prefix": db.get("prefix"),
                    "suffix": db.get("suffix"),
                }
                allowed = db.get("types")
                dedupe = db.get("dedupe")
                copied = 0
                for m in sorted(
                    (x for x in msgs if x is not None and not getattr(x, "empty", False)),
                    key=lambda x: x.id,
                ):
                    if self.stop_event.is_set() or not db.get("on2"):
                        break
                    mid = m.id
                    mtype, media = media_of(m)
                    if mtype not in allowed:
                        db.q2_drop(src, mid)
                        self.skipped += 1
                        continue
                    fuid, fname, fsize = file_info(media)
                    if dedupe and fuid and db.file_seen(fuid):
                        db.q2_drop(src, mid)
                        self.skipped += 1
                        continue
                    caption = apply_caption(
                        caption_html_of(m),
                        filename=fname,
                        size=fsize,
                        msg_id=mid,
                        settings=cap_cfg,
                    )
                    ok = await self._copy_msg(m, dest, caption)
                    db.q2_drop(src, mid)
                    if ok:
                        if fuid:
                            db.file_add(fuid)
                        self.sent += 1
                        db.stat_incr("total_sent")
                        copied += 1
                        self._pacer.configure(
                            delay=db.get("delay"),
                            batch=db.get("batch"),
                            wait=db.get("wait"),
                        )
                        try:
                            await self._pacer.tick()
                        except StopRequested:
                            return
                    else:
                        self.failed += 1
                        db.stat_incr("total_failed")
                if copied == 0 and not self.stop_event.is_set():
                    await self._sleep(4)
        except FatalCopyError as e:
            db.set("on2", False)
            await self._notify(f"⛔ Source B stopped (<code>{e}</code>).")
        except StopRequested:
            pass
        finally:
            self.state = "idle"

    # -- status ---------------------------------------------------------
    def snapshot(self) -> dict:
        db = self.db
        src = db.get("src2")
        return {
            "state": self.state,
            "on": db.get("on2"),
            "configured": bool(src),
            "queue": db.q2_count(src) if src else 0,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "batch_n": self._pacer.count,
        }
