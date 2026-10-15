"""Shared fakes for engine tests — no network, no Telegram."""

from bot.db import DB


class FakeMedia:
    def __init__(self, file_unique_id="uid", file_name="file.bin", file_size=100):
        self.file_unique_id = file_unique_id
        self.file_name = file_name
        self.file_size = file_size


class FakeCaption:
    def __init__(self, html):
        self.html = html


class FakeMessage:
    def __init__(self, id, media_type="document", media=None,
                 caption_html=None, empty=False, service=False, log=None):
        self.id = id
        self.empty = empty
        self.service = service
        self._log = log if log is not None else []
        for attr in ("document", "video", "audio", "photo",
                     "animation", "voice", "video_note", "sticker"):
            setattr(self, attr, None)
        if media_type and media is not None:
            setattr(self, media_type, media)
        self.caption = FakeCaption(caption_html) if caption_html is not None else None

    async def copy(self, dest, caption=None, parse_mode=None):
        self._log.append({"id": self.id, "dest": dest, "caption": caption})
        return self


class FakeChat:
    def __init__(self, id=1, title="chat", has_protected_content=False):
        self.id = id
        self.title = title
        self.has_protected_content = has_protected_content


class FakeClient:
    def __init__(self, messages=(), protected=False):
        self.messages = {m.id: m for m in messages}
        self.protected = protected

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, int):
            ids = [ids]
        return [self.messages[i] for i in ids if i in self.messages]

    def get_chat_history(self, chat_id, limit=1):
        msgs = self

        async def gen():
            ordered = sorted(msgs.messages.values(),
                             key=lambda m: m.id, reverse=True)
            for m in ordered[:limit]:
                yield m

        return gen()

    async def get_chat(self, chat_id):
        return FakeChat(id=chat_id, has_protected_content=self.protected)


def make_db(**overrides):
    db = DB(":memory:")
    db.set("src", 111)
    db.set("dest", 222)
    db.set("delay", 0)
    db.set("batch", 1000)
    db.set("wait", 0)
    for k, v in overrides.items():
        db.set(k, v)
    return db


def instant_sleep_factory(calls):
    async def _sleep(secs):
        calls.append(secs)
        import asyncio
        await asyncio.sleep(0)

    return _sleep


def msg(id, fuid=None, media_type="document", caption=None, log=None):
    media = FakeMedia(file_unique_id=fuid or f"uid-{id}",
                      file_name=f"file{id}.mp4", file_size=1024)
    return FakeMessage(id, media_type=media_type, media=media,
                       caption_html=caption, log=log)
