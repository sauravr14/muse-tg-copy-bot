"""Source A copy engine: reads via the user session, copies in ascending id order.

No pyrofork imports here — Telegram errors are duck-typed so the engine is
fully unit-testable with fake clients.
"""

import asyncio
import logging
import re
import time

from .live import wait_for_new
from .utils import (
    Pacer,
    StopRequested,
    apply_caption,
    caption_html_of,
    file_info,
    interruptible_sleep,
    media_of,
)

log = logging.getLogger("copybot.engine")

FATAL_ERRORS = {
    "CHAT_WRITE_FORBIDDEN",
    "CHAT_ADMIN_REQUIRED",
    "CHANNEL_PRIVATE",
    "CHANNEL_INVALID",
    "USER_BANNED_IN_CHANNEL",
    "PEER_ID_INVALID",
    "CHAT_FORWARDS_RESTRICTED",
    "USER_DEACTIVATED",
    "AUTH_KEY_UNREGISTERED",
    "SESSION_REVOKED",
}

_FLOOD_NAMES = ("FloodWait", "SlowmodeWait")
_CHUNK = 100
_SCAN_THROTTLE = 4  # seconds after a 100-id chunk with zero copies
_NET_RETRY = 5


class FatalCopyError(Exception):
    pass


def _err_code(e) -> str:
    return getattr(e, "ID", None) or type(e).__name__


def _flood_secs(e) -> int:
    return (getattr(e, "value", 0) or 0) + 1


class CopyEngine:
    """Ascending-id copy loop for one source/destination pair (Source A)."""

    def __init__(self, db, client, notify=None, name="A", sleeper=None):
        self.db = db
        self.client = client  # None when SESSION_STRING is absent
        self.notify = notify
        self.name = name
        self.stop_event = asyncio.Event()
        self._sleeper = sleeper  # injected fake sleep for tests
        self._task = None
        self._restart_requested = False
        self._pacer = Pacer(sleep=self._make_sleep())
        self._reset_stats()

    # -- lifecycle ------------------------------------------------------
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
        self.state_secs = 0
        self.position = 0
        self.total = 0
        self._latest = 0
        self._pacer.count = 0

    def start(self) -> bool:
        if self._task and not self._task.done():
            return False
        self.stop_event.clear()
        self._restart_requested = False
        self._pacer.sleep = self._make_sleep()
        self._task = asyncio.create_task(
            self._supervisor(), name=f"copy-engine-{self.name}"
        )
        return True

    async def _supervisor(self):
        while True:
            self.stop_event.clear()
            self._restart_requested = False
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("engine %s crashed", self.name)
                await self._notify(f"Engine {self.name} crashed — see logs.")
            if not self._restart_requested:
                break

    def request_restart(self):
        """Cleanly restart the loop (used when source/dest changes)."""
        self._restart_requested = True
        self.stop_event.set()

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

    # -- guarded API calls ----------------------------------------------
    async def _guard(self, coro):
        """Await coro with FloodWait/network retries. Returns None on soft fail."""
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
                    self.state = "flood-wait"
                    self.state_secs = _flood_secs(e)
                    await self._sleep(self.state_secs)
                    continue
                if _err_code(e) in FATAL_ERRORS:
                    raise FatalCopyError(_err_code(e))
                if isinstance(e, (OSError, asyncio.TimeoutError)):
                    await self._sleep(_NET_RETRY)
                    continue
                log.warning("API soft error: %r", e)
                return None

    async def _latest_id(self, src):
        floods = 0
        while True:
            try:
                async for m in self.client.get_chat_history(src, limit=1):
                    return m.id
                return 0
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
                log.warning("latest_id soft error: %r", e)
                return None

    # -- single message copy --------------------------------------------
    async def _copy_msg(self, msg, dest, caption) -> bool:
        """True=ok, False=counted failure. Raises FatalCopyError / StopRequested."""
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
                    self.state = "flood-wait"
                    self.state_secs = _flood_secs(e)
                    await self._sleep(self.state_secs)
                    continue
                code = _err_code(e)
                if code == "CAPTION_TOO_LONG" and not fixed:
                    fixed = True
                    cap = re.sub(r"<[^>]+>", "", cap or "")[:1024]
                    continue
                if code in FATAL_ERRORS:
                    raise FatalCopyError(code)
                if isinstance(e, (OSError, asyncio.TimeoutError)):
                    await self._sleep(_NET_RETRY)
                    continue
                log.exception("copy of msg %s failed (%s)", getattr(msg, "id", "?"), code)
                return False

    # -- main loop -------------------------------------------------------
    async def run(self):
        db = self.db
        if self.client is None:
            await self._notify(
                "Source A needs <b>SESSION_STRING</b> — set it in .env to enable."
            )
            db.set("running", False)
            return

        src = db.get("src")
        dest = db.get("dest")
        if not src or not dest:
            await self._notify("Set source and destination first: /setsource, /setdest.")
            db.set("running", False)
            return
        if str(src) == str(dest):
            await self._notify("Source and destination can't be the same chat.")
            db.set("running", False)
            return

        # protected-content check at copy start
        try:
            chat = await self._guard(self.client.get_chat(src))
        except (FatalCopyError, StopRequested):
            db.set("running", False)
            raise
        if chat is not None and getattr(chat, "has_protected_content", False):
            await self._notify(
                "Source has “Restrict saving content” enabled — copy refused."
            )
            db.set("running", False)
            return

        db.set("running", True)
        self._reset_stats()
        try:
            await self._loop()
        except FatalCopyError as e:
            db.set("running", False)
            await self._notify(
                f"⛔ Copy stopped (<code>{e}</code>). Progress is saved — fix it and /resume."
            )
        except StopRequested:
            pass
        finally:
            self.state = "idle"

    def _caption_settings(self):
        db = self.db
        return {
            "nocaption": db.get("nocaption"),
            "remove_links": db.get("remove_links"),
            "remove_usernames": db.get("remove_usernames"),
            "blocklist": db.get("blocklist"),
            "replacements": db.get("replacements"),
            "template": db.get("template"),
            "prefix": db.get("prefix"),
            "suffix": db.get("suffix"),
        }

    async def _loop(self):
        db = self.db
        latest = None  # refreshed only when caught up (fewer API calls)
        while not self.stop_event.is_set():
            if db.get("paused"):
                self.state = "paused"
                await self._sleep(1)
                continue

            src = db.get("src")
            dest = db.get("dest")
            if not src or not dest or str(src) == str(dest):
                db.set("running", False)
                return

            last = db.progress_get(src)
            end = db.get("end_id") or 0
            if latest is None or last >= latest:
                latest = await self._latest_id(src)
                if latest is None:
                    self.state = "scanning"
                    await self._sleep(5)
                    continue
                if end:
                    latest = min(latest, end)

            self.total = max(0, latest - last)
            self._latest = latest
            if last >= latest:
                if db.get("live"):
                    self.state = "watching"
                    try:
                        latest = await wait_for_new(
                            lambda: self._latest_id(src),
                            last,
                            self._sleep,
                            self.stop_event,
                        )
                    except StopRequested:
                        return
                    if end:
                        latest = min(latest, end)
                    continue
                db.set("running", False)
                await self._notify(
                    f"✅ Done — copied <b>{self.sent}</b>, skipped {self.skipped}, "
                    f"failed {self.failed}."
                )
                return

            start = last + 1
            ids = list(range(start, min(start + _CHUNK, latest + 1)))
            self.state = "scanning"
            msgs = await self._guard(self.client.get_messages(src, ids))
            if msgs is None:
                await self._sleep(5)
                continue

            copied_in_chunk = 0
            cap_cfg = self._caption_settings()
            allowed = db.get("types")
            dedupe = db.get("dedupe")
            for m in sorted((x for x in msgs if x is not None), key=lambda x: x.id):
                if self.stop_event.is_set() or db.get("paused"):
                    break
                mid = m.id
                if end and mid > end:
                    db.progress_set(src, mid)
                    continue
                self.position = mid
                if getattr(m, "empty", False):
                    db.progress_set(src, mid)
                    continue
                mtype, media = media_of(m)
                if mtype not in allowed:
                    db.progress_set(src, mid)
                    self.skipped += 1
                    continue
                fuid, fname, fsize = file_info(media)
                if dedupe and fuid and db.file_seen(fuid):
                    db.progress_set(src, mid)
                    self.skipped += 1
                    continue

                self.state = "copying"
                caption = apply_caption(
                    caption_html_of(m),
                    filename=fname,
                    size=fsize,
                    msg_id=mid,
                    settings=cap_cfg,
                )
                ok = await self._copy_msg(m, dest, caption)
                db.progress_set(src, mid)  # saved after every message
                if ok:
                    if fuid:
                        db.file_add(fuid)
                    self.sent += 1
                    db.stat_incr("total_sent")
                    copied_in_chunk += 1
                    self._pacer.configure(
                        delay=db.get("delay"),
                        batch=db.get("batch"),
                        wait=db.get("wait"),
                    )
                    self.state = (
                        "cooldown" if self._pacer.count + 1 >= max(1, db.get("batch")) else "gap"
                    )
                    try:
                        await self._pacer.tick()
                    except StopRequested:
                        return
                else:
                    self.failed += 1
                    db.stat_incr("total_failed")

            if copied_in_chunk == 0 and not self.stop_event.is_set():
                # scanning throttle: long stretches of non-matching ids
                # caused FloodWait on channels.GetMessages without this
                self.state = "scanning"
                await self._sleep(_SCAN_THROTTLE)

    # -- status snapshot --------------------------------------------------
    def snapshot(self) -> dict:
        elapsed = time.time() - self.started_at
        speed = (self.sent / elapsed * 60) if elapsed > 0 else 0.0
        remaining = max(0, self._latest - self.position)
        eta = (remaining / speed * 60) if speed > 0 else 0
        return {
            "state": self.state,
            "state_secs": self.state_secs,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "position": self.position,
            "total": self.total,
            "batch_n": self._pacer.count,
            "batch": self.db.get("batch"),
            "delay": self.db.get("delay"),
            "wait": self.db.get("wait"),
            "speed": speed,
            "eta": eta,
            "elapsed": elapsed,
            "all_sent": self.db.stat_get("total_sent"),
            "all_failed": self.db.stat_get("total_failed"),
            "paused": self.db.get("paused"),
            "running": self.is_running or self.db.get("running"),
        }
