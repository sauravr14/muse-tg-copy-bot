"""Shared helpers: caption pipeline, pacing, media detection, parsing."""

import asyncio
import html as _html
import re

# --------------------------------------------------------------------------
# Caption processing — applied in the exact spec order, on message.caption.html
# --------------------------------------------------------------------------

_ANCHOR_RE = re.compile(r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_URL_RE = re.compile(
    r"https?://\S+|www\.\S+|(?:t\.me|telegram\.me|telegram\.dog)/\S+", re.I
)
_USER_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{5,32})(?![\w])")
_EMPTY_TAG_RE = re.compile(
    r"<(b|i|u|s|strike|del|code|pre|a|span|blockquote)(?:\s[^>]*)?>\s*</\1>",
    re.I,
)


def human_size(n) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _unwrap_anchors(html_text: str, only_telegram: bool) -> str:
    """Replace <a href> anchors with their inner text.

    All anchors when removing links; only t.me / tg:// anchors when only
    removing usernames.
    """

    def _rep(m):
        href, inner = m.group(1), m.group(2)
        if only_telegram and not ("t.me" in href or href.startswith("tg://")):
            return m.group(0)
        return inner

    return _ANCHOR_RE.sub(_rep, html_text)


def apply_caption(
    caption_html,
    *,
    filename="",
    size=0,
    msg_id=0,
    settings=None,
) -> str:
    """Run the caption pipeline in spec order. Returns HTML (may be '')."""
    s = settings or {}
    text = caption_html or ""

    # 1. nocaption -> empty, skip everything
    if s.get("nocaption"):
        return ""

    remove_links = bool(s.get("remove_links"))
    remove_users = bool(s.get("remove_usernames"))

    # 2. unwrap anchors
    if remove_links or remove_users:
        text = _unwrap_anchors(text, only_telegram=(not remove_links and remove_users))

    # 3. remove links
    if remove_links:
        text = _URL_RE.sub("", text)

    # 4. remove @usernames (5-32 chars, not part of a word/URL)
    if remove_users:
        text = _USER_RE.sub("", text)

    # 5. blocklist (case-insensitive)
    for word in s.get("blocklist") or []:
        if word:
            text = re.sub(re.escape(word), "", text, flags=re.I)

    # 6. replacements (case-insensitive)
    for old, new in (s.get("replacements") or {}).items():
        if old:
            text = re.sub(re.escape(old), new, text, flags=re.I)

    # 7. cleanup
    text = _EMPTY_TAG_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # 8. template
    template = s.get("template") or ""
    if template:
        values = {
            "caption": text,
            "filename": _html.escape(filename or ""),
            "size": human_size(size),
            "id": msg_id,
        }

        class _Safe(dict):
            def __missing__(self, key):
                return "{" + key + "}"  # unknown placeholders stay literal

        try:
            text = template.format_map(_Safe(values))
        except Exception:
            pass  # bad format string -> fall back to raw text

    # 9. prefix / suffix
    prefix = s.get("prefix") or ""
    suffix = s.get("suffix") or ""
    if prefix:
        text = prefix + ("\n" + text if text else "")
    if suffix:
        text = text + ("\n" + suffix if text else "")

    return text


# --------------------------------------------------------------------------
# Pacing — delay between files, longer wait after every `batch` files
# --------------------------------------------------------------------------


class StopRequested(Exception):
    """Raised when an interruptible sleep is cut short by /stop."""


def interruptible_sleep(stop_event):
    """Return an async sleep(secs) that raises StopRequested if stop_event fires."""

    async def _sleep(secs):
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0, secs))
        except asyncio.TimeoutError:
            return
        raise StopRequested()

    return _sleep


class Pacer:
    """Counts copies; sleeps `delay` after each, `wait` instead every `batch`.

    `sleep` is an injectable async callable so tests can use a fake clock.
    """

    def __init__(self, delay=3, batch=20, wait=30, sleep=None):
        self.delay = delay
        self.batch = batch
        self.wait = wait
        self.sleep = sleep or (lambda s: asyncio.sleep(s))
        self.count = 0
        self.last_kind = ""  # "gap" | "cooldown"
        self.last_secs = 0.0
        self.deadline = 0.0  # loop-time when the current wait ends

    def configure(self, delay=None, batch=None, wait=None):
        if delay is not None:
            self.delay = delay
        if batch is not None:
            self.batch = batch
        if wait is not None:
            self.wait = wait

    async def tick(self):
        """Call after each successful copy."""
        self.count += 1
        batch = max(1, int(self.batch))
        if self.count >= batch:
            self.count = 0
            kind, secs = "cooldown", self.wait
        else:
            kind, secs = "gap", self.delay
        self.last_kind = kind
        self.last_secs = secs
        try:
            self.deadline = asyncio.get_running_loop().time() + max(0, secs)
        except RuntimeError:
            self.deadline = 0.0
        await self.sleep(secs)

    def remaining(self) -> float:
        """Seconds left in the current gap/cooldown (0 when idle)."""
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return 0.0
        return max(0.0, self.deadline - now)


# --------------------------------------------------------------------------
# Media helpers
# --------------------------------------------------------------------------

MEDIA_ATTRS = (
    "document",
    "video",
    "audio",
    "photo",
    "animation",
    "voice",
    "video_note",
    "sticker",
)

# extra types the user may enable via /types (not in the default list)
EXTRA_TYPES = ("video_note", "sticker")


def media_of(msg):
    """Return (type_name, media_obj) for the first media attr present, else (None, None)."""
    for attr in MEDIA_ATTRS:
        media = getattr(msg, attr, None)
        if media:
            return attr, media
    return None, None


def file_info(media):
    """Return (file_unique_id, file_name, file_size) for a media object."""
    return (
        getattr(media, "file_unique_id", None),
        getattr(media, "file_name", None) or "",
        getattr(media, "file_size", 0) or 0,
    )


def caption_html_of(msg) -> str:
    cap = getattr(msg, "caption", None)
    if cap is None:
        return ""
    html = getattr(cap, "html", None)
    return html if html is not None else str(cap)


# --------------------------------------------------------------------------
# Command-argument parsing
# --------------------------------------------------------------------------

_TME_RE = re.compile(r"^https?://t\.me/([A-Za-z0-9_]{5,})/?", re.I)


def parse_target(text):
    """Parse <id | @username | t.me link> into int or @username string."""
    t = (text or "").strip()
    m = _TME_RE.match(t)
    if m:
        return "@" + m.group(1)
    if t.startswith("@"):
        return t
    try:
        return int(t)
    except ValueError:
        return t


def parse_on_off(text):
    t = (text or "").strip().lower()
    if t in ("on", "yes", "1", "true"):
        return True
    if t in ("off", "no", "0", "false"):
        return False
    return None


def utc_now_str() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
