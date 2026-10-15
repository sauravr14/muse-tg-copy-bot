"""All bot commands, the live /status panel, and the Source B live-capture handler."""

import asyncio
import logging
import time

from pyrogram import filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .utils import MEDIA_ATTRS, parse_on_off, parse_target, utc_now_str

log = logging.getLogger("copybot.handlers")

NO_SESSION_MSG = (
    "Source A needs <b>SESSION_STRING</b> — set it in <code>.env</code> "
    "to enable. Source B works without it."
)


def register(bot, user, ctx):
    """Attach every handler. Safe to call with fake (never started) clients."""
    cfg, db = ctx.cfg, ctx.db
    eng, eng2 = ctx.eng_a, ctx.eng_b
    OWNER = cfg.owner_id

    # ------------------------------------------------------------------
    async def _owner(_, __, m):
        u = getattr(m, "from_user", None)
        chat = getattr(m, "chat", None)
        return (
            bool(u)
            and u.id == OWNER
            and chat is not None
            and chat.type == ChatType.PRIVATE
        )

    owner_only = filters.create(_owner)

    async def _reply(message, html):
        try:
            await message.reply_text(
                html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except Exception:
            log.warning("reply failed", exc_info=True)

    def _args(message):
        cmd = message.command or []
        return cmd[1:]

    def _usage(cmd, hint):
        return f"Usage: <code>{cmd}</code>\n{hint}"

    async def _resolve(client, target, message, what="chat"):
        try:
            return await client.get_chat(target)
        except Exception as e:
            await _reply(
                message,
                f"Couldn't resolve that {what} ({type(e).__name__}). "
                "Use a numeric id, @username or t.me link.",
            )
            return None

    async def _need_user(message):
        if user is None:
            await _reply(message, NO_SESSION_MSG)
            return None
        return user

    # ------------------------------------------------------------------
    @bot.on_message(filters.private & filters.command("start") & owner_only)
    async def _start(_, message):
        await _reply(
            message,
            "👋 <b>Channel Copy Bot</b>\n"
            "Copies files (no forward tag) at a controlled pace.\n"
            "Use /help for commands, /status for the live panel.",
        )

    @bot.on_message(filters.private & filters.command("help") & owner_only)
    async def _help(_, message):
        await _reply(
            message,
            "<b>Setup</b>\n"
            "/setsource, /setdest, /setskip, /setend\n"
            "<b>Run</b>\n"
            "/startcopy (/copy), /pause, /resume, /stop, /status, /live on|off, /resetprogress\n"
            "<b>Pacing</b>\n"
            "/setdelay, /setbatch, /setwait\n"
            "<b>Caption</b>\n"
            "/setcaption, /setprefix, /setsuffix, /nocaption, /removeat, /removelinks,\n"
            "/addblock, /delblock, /blocklist, /replace, /delreplace, /replacelist\n"
            "<b>Filters</b>\n"
            "/types, /dedupe\n"
            "<b>Source B</b> (bot is admin)\n"
            "/setsource2, /setdest2, /source2 on|off, /backfill2, /queue2\n"
            "<b>Info</b>\n"
            "/settings, /resetsettings",
        )

    # ---------------- setup ----------------
    @bot.on_message(
        filters.private & filters.command(["setsource", "setsourcechannel"]) & owner_only
    )
    async def _setsource(_, message):
        if await _need_user(message) is None:
            return
        args = _args(message)
        if not args:
            return await _reply(message, _usage("/setsource <id | @username | t.me link>", "Source A: channel/group you can read."))
        chat = await _resolve(user, parse_target(args[0]), message, "source")
        if not chat:
            return
        if getattr(chat, "has_protected_content", False):
            return await _reply(
                message,
                "🚫 That chat has <b>“Restrict saving content”</b> enabled — "
                "I can't copy from it. Pick another source.",
            )
        if str(chat.id) == str(db.get("dest")):
            return await _reply(message, "Source and destination can't be the same chat.")
        db.set("src", chat.id)
        pos = db.progress_get(chat.id)
        await _reply(
            message,
            f"✅ Source: <b>{chat.title or chat.id}</b> (<code>{chat.id}</code>)\n"
            f"Resume position: msg {pos}",
        )
        if eng and eng.is_running:
            eng.request_restart()

    @bot.on_message(
        filters.private
        & filters.command(["setdest", "setdestination", "setdestinationchannel"])
        & owner_only
    )
    async def _setdest(_, message):
        if await _need_user(message) is None:
            return
        args = _args(message)
        if not args:
            return await _reply(message, _usage("/setdest <id | @username | link>", "Where copied files are posted."))
        chat = await _resolve(user, parse_target(args[0]), message, "destination")
        if not chat:
            return
        if str(chat.id) == str(db.get("src")):
            return await _reply(message, "Source and destination can't be the same chat.")
        warn = ""
        try:
            me = await user.get_me()
            member = await user.get_chat_member(chat.id, me.id)
            if getattr(member, "status", "") not in ("administrator", "owner"):
                warn = "\n⚠️ Your account doesn't look like admin there — posting may fail."
        except Exception:
            pass
        db.set("dest", chat.id)
        await _reply(
            message,
            f"✅ Destination: <b>{chat.title or chat.id}</b> (<code>{chat.id}</code>){warn}",
        )
        if eng and eng.is_running:
            eng.request_restart()

    @bot.on_message(filters.private & filters.command("setskip") & owner_only)
    async def _setskip(_, message):
        args = _args(message)
        if not args or not args[0].isdigit():
            return await _reply(message, _usage("/setskip <msg_id>", "Continue after this message id (current source)."))
        src = db.get("src")
        if not src:
            return await _reply(message, "Set a source first (/setsource).")
        db.progress_set(src, int(args[0]))
        await _reply(message, f"⏭ Will continue after msg <code>{args[0]}</code>.")
        if eng and eng.is_running:
            eng.request_restart()

    @bot.on_message(filters.private & filters.command("setend") & owner_only)
    async def _setend(_, message):
        args = _args(message)
        if not args or not args[0].isdigit():
            return await _reply(message, _usage("/setend <msg_id | 0>", "Stop at this id (0 = latest)."))
        db.set("end_id", int(args[0]))
        await _reply(
            message,
            "🏁 End id cleared (will run to latest)." if args[0] == "0"
            else f"🏁 Will stop at msg <code>{args[0]}</code>.",
        )

    # ---------------- run control ----------------
    @bot.on_message(filters.private & filters.command(["startcopy", "copy"]) & owner_only)
    async def _startcopy(_, message):
        if await _need_user(message) is None:
            return
        if not db.get("src") or not db.get("dest"):
            return await _reply(message, "Set source and destination first: /setsource, /setdest.")
        if eng.is_running:
            return await _reply(message, "Already running — see /status.")
        db.set("running", True)
        db.set("paused", False)
        eng.start()
        await _reply(message, "▶️ Copy started. /status for the live panel.")

    @bot.on_message(filters.private & filters.command("pause") & owner_only)
    async def _pause(_, message):
        db.set("paused", True)
        await _reply(message, "⏸ Paused. /resume to continue.")

    @bot.on_message(filters.private & filters.command("resume") & owner_only)
    async def _resume(_, message):
        db.set("paused", False)
        if eng and user is not None and not eng.is_running and db.get("running"):
            eng.start()
        await _reply(message, "▶️ Resumed.")

    @bot.on_message(filters.private & filters.command("stop") & owner_only)
    async def _stop(_, message):
        db.set("running", False)
        db.set("paused", False)
        if eng:
            await eng.stop()
        await _reply(message, "⏹ Stopped. Progress is saved — /startcopy to run again.")

    @bot.on_message(filters.private & filters.command(["live"]) & owner_only)
    async def _live(_, message):
        args = _args(message)
        v = parse_on_off(args[0]) if args else None
        if v is None:
            return await _reply(
                message,
                _usage("/live on|off", f"Now: <b>{'on' if db.get('live') else 'off'}</b> — keep watching for new files after catching up."),
            )
        db.set("live", v)
        await _reply(message, f"📡 Live mode <b>{'on' if v else 'off'}</b>.")

    @bot.on_message(filters.private & filters.command("resetprogress") & owner_only)
    async def _resetprogress(_, message):
        if (eng and eng.is_running) or db.get("running"):
            return await _reply(message, "Stop the copy first (/stop).")
        src = db.get("src")
        if src:
            db.progress_clear(src)
        db.files_clear()
        await _reply(message, "🗑 Progress and duplicate memory cleared.")

    # ---------------- pacing ----------------
    async def _set_num(message, key, label, minimum=1):
        args = _args(message)
        if not args or not args[0].isdigit() or int(args[0]) < minimum:
            return await _reply(
                message,
                _usage(f"/set{key} <n>", f"Now: <b>{db.get(key)}</b> (min {minimum})."),
            )
        db.set(key, int(args[0]))
        await _reply(message, f"✅ {label} = <b>{args[0]}</b> (applies immediately).")

    @bot.on_message(filters.private & filters.command("setdelay") & owner_only)
    async def _setdelay(_, message):
        await _set_num(message, "delay", "Delay between files (sec)", 0)

    @bot.on_message(filters.private & filters.command("setbatch") & owner_only)
    async def _setbatch(_, message):
        await _set_num(message, "batch", "Files per batch", 1)

    @bot.on_message(filters.private & filters.command("setwait") & owner_only)
    async def _setwait(_, message):
        await _set_num(message, "wait", "Cooldown after each batch (sec)", 0)

    # ---------------- caption ----------------
    @bot.on_message(filters.private & filters.command("setcaption") & owner_only)
    async def _setcaption(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2:
            return await _reply(
                message,
                _usage(
                    "/setcaption <text | off>",
                    "Template for every caption. Placeholders: {caption} {filename} {size} {id}. HTML allowed.",
                ),
            )
        text = raw[1]
        db.set("template", "" if text.strip().lower() == "off" else text)
        await _reply(message, "✅ Caption template updated." if text.strip().lower() != "off" else "✅ Caption template cleared.")

    @bot.on_message(filters.private & filters.command("setprefix") & owner_only)
    async def _setprefix(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2:
            return await _reply(message, _usage("/setprefix <text | off>", "Line added above the caption. HTML allowed."))
        text = raw[1]
        db.set("prefix", "" if text.strip().lower() == "off" else text)
        await _reply(message, "✅ Prefix updated.")

    @bot.on_message(filters.private & filters.command("setsuffix") & owner_only)
    async def _setsuffix(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2:
            return await _reply(message, _usage("/setsuffix <text | off>", "Line added below the caption. HTML allowed."))
        text = raw[1]
        db.set("suffix", "" if text.strip().lower() == "off" else text)
        await _reply(message, "✅ Suffix updated.")

    @bot.on_message(filters.private & filters.command("nocaption") & owner_only)
    async def _nocaption(_, message):
        args = _args(message)
        v = parse_on_off(args[0]) if args else None
        if v is None:
            return await _reply(message, _usage("/nocaption on|off", f"Now: <b>{'on' if db.get('nocaption') else 'off'}</b>."))
        db.set("nocaption", v)
        await _reply(message, f"✅ Captions <b>{'stripped' if v else 'kept'}</b>.")

    @bot.on_message(
        filters.private & filters.command(["removeat", "removeusername"]) & owner_only
    )
    async def _removeat(_, message):
        args = _args(message)
        v = parse_on_off(args[0]) if args else None
        if v is None:
            return await _reply(message, _usage("/removeat on|off", f"Now: <b>{'on' if db.get('remove_usernames') else 'off'}</b>."))
        db.set("remove_usernames", v)
        await _reply(message, f"✅ @username removal <b>{'on' if v else 'off'}</b>.")

    @bot.on_message(filters.private & filters.command("removelinks") & owner_only)
    async def _removelinks(_, message):
        args = _args(message)
        v = parse_on_off(args[0]) if args else None
        if v is None:
            return await _reply(message, _usage("/removelinks on|off", f"Now: <b>{'on' if db.get('remove_links') else 'off'}</b>."))
        db.set("remove_links", v)
        await _reply(message, f"✅ Link removal <b>{'on' if v else 'off'}</b>.")

    @bot.on_message(filters.private & filters.command("addblock") & owner_only)
    async def _addblock(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2:
            return await _reply(message, _usage("/addblock <word or phrase>", "Removed from captions (case-insensitive)."))
        bl = db.get("blocklist")
        if raw[1] not in bl:
            bl.append(raw[1])
            db.set("blocklist", bl)
        await _reply(message, f"✅ Blocked: <code>{raw[1]}</code> ({len(bl)} total).")

    @bot.on_message(filters.private & filters.command("delblock") & owner_only)
    async def _delblock(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2:
            return await _reply(message, _usage("/delblock <word or phrase>", ""))
        bl = [w for w in db.get("blocklist") if w != raw[1]]
        db.set("blocklist", bl)
        await _reply(message, "✅ Removed from blocklist.")

    @bot.on_message(filters.private & filters.command("blocklist") & owner_only)
    async def _blocklist(_, message):
        bl = db.get("blocklist")
        await _reply(
            message,
            "🚫 Blocklist is empty." if not bl
            else "🚫 Blocklist:\n" + "\n".join(f"• <code>{w}</code>" for w in bl),
        )

    @bot.on_message(filters.private & filters.command("replace") & owner_only)
    async def _replace(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2 or "|" not in raw[1]:
            return await _reply(message, _usage("/replace <old> | <new>", "Case-insensitive text replacement in captions."))
        old, new = (p.strip() for p in raw[1].split("|", 1))
        if not old:
            return await _reply(message, "The <old> text can't be empty.")
        rp = db.get("replacements")
        rp[old] = new
        db.set("replacements", rp)
        await _reply(message, f"✅ <code>{old}</code> → <code>{new}</code>")

    @bot.on_message(filters.private & filters.command("delreplace") & owner_only)
    async def _delreplace(_, message):
        raw = message.text.split(None, 1)
        if len(raw) < 2:
            return await _reply(message, _usage("/delreplace <old>", ""))
        rp = db.get("replacements")
        rp.pop(raw[1], None)
        db.set("replacements", rp)
        await _reply(message, "✅ Replacement removed.")

    @bot.on_message(filters.private & filters.command("replacelist") & owner_only)
    async def _replacelist(_, message):
        rp = db.get("replacements")
        await _reply(
            message,
            "🔁 No replacements." if not rp
            else "🔁 Replacements:\n" + "\n".join(f"• <code>{k}</code> → <code>{v}</code>" for k, v in rp.items()),
        )

    # ---------------- filters ----------------
    @bot.on_message(filters.private & filters.command("types") & owner_only)
    async def _types(_, message):
        args = _args(message)
        if not args:
            return await _reply(
                message,
                _usage(
                    "/types <list | all>",
                    f"Now: <code>{' '.join(db.get('types'))}</code>\nAllowed: <code>{' '.join(MEDIA_ATTRS)}</code>",
                ),
            )
        if args[0].lower() == "all":
            chosen = list(MEDIA_ATTRS)
        else:
            chosen = [t.lower() for t in args if t.lower() in MEDIA_ATTRS]
            bad = [t for t in args if t.lower() not in MEDIA_ATTRS]
            if bad:
                return await _reply(
                    message,
                    f"Unknown type(s): <code>{' '.join(bad)}</code>\nAllowed: <code>{' '.join(MEDIA_ATTRS)}</code>",
                )
            if not chosen:
                return await _reply(message, "Pick at least one type.")
        db.set("types", chosen)
        await _reply(message, f"✅ Types: <code>{' '.join(chosen)}</code>")

    @bot.on_message(filters.private & filters.command("dedupe") & owner_only)
    async def _dedupe(_, message):
        args = _args(message)
        v = parse_on_off(args[0]) if args else None
        if v is None:
            return await _reply(message, _usage("/dedupe on|off", f"Now: <b>{'on' if db.get('dedupe') else 'off'}</b> — skip already-copied files."))
        db.set("dedupe", v)
        await _reply(message, f"✅ Duplicate protection <b>{'on' if v else 'off'}</b>.")

    # ---------------- info / reset ----------------
    @bot.on_message(filters.private & filters.command("settings") & owner_only)
    async def _settings(_, message):
        dest2 = db.get("dest2")
        await _reply(
            message,
            "<b>⚙️ Settings</b>\n"
            f"Source A: <code>{db.get('src') or '—'}</code> → <code>{db.get('dest') or '—'}</code>\n"
            f"Source B: <code>{db.get('src2') or '—'}</code> → <code>{dest2 or 'main'}</code> "
            f"({'on' if db.get('on2') else 'off'})\n"
            f"Pacing: {db.get('delay')}s gap · {db.get('batch')} files · {db.get('wait')}s cooldown\n"
            f"Types: <code>{' '.join(db.get('types'))}</code> · dedupe {'on' if db.get('dedupe') else 'off'}\n"
            f"Live A: {'on' if db.get('live') else 'off'} · end id: {db.get('end_id') or 'latest'}\n"
            f"Caption: {'stripped' if db.get('nocaption') else 'kept'} · "
            f"@user {'on' if db.get('remove_usernames') else 'off'} · "
            f"links {'on' if db.get('remove_links') else 'off'}\n"
            f"Template: {'set' if db.get('template') else '—'} · prefix: {'set' if db.get('prefix') else '—'} · "
            f"suffix: {'set' if db.get('suffix') else '—'}\n"
            f"Blocklist: {len(db.get('blocklist'))} · replacements: {len(db.get('replacements'))}\n"
            f"Session: {'set (Source A ready)' if cfg.has_session else 'absent (Source B only)'}",
        )

    @bot.on_message(filters.private & filters.command("resetsettings") & owner_only)
    async def _resetsettings(_, message):
        if (eng and eng.is_running) or db.get("running"):
            return await _reply(message, "Stop the copy first (/stop).")
        db.reset_settings()
        await _reply(message, "✅ Settings reset to defaults (progress kept).")

    # ---------------- Source B ----------------
    @bot.on_message(filters.private & filters.command("setsource2") & owner_only)
    async def _setsource2(_, message):
        args = _args(message)
        if not args:
            return await _reply(
                message,
                _usage(
                    "/setsource2 <id | @username>",
                    "Source B: a channel/group where <b>this bot is admin</b>.",
                ),
            )
        target = parse_target(args[0])
        try:
            chat = await bot.get_chat(target)
        except Exception:
            return await _reply(
                message,
                "I can't see that chat. Add me as <b>admin</b> there, then "
                "<b>forward any post from that channel to me here</b> (or use its @username).",
            )
        if getattr(chat, "has_protected_content", False):
            return await _reply(
                message,
                "🚫 That chat has <b>“Restrict saving content”</b> enabled — I can't copy from it.",
            )
        dest2 = db.get("dest2") or db.get("dest")
        if str(chat.id) == str(dest2):
            return await _reply(message, "Source and destination can't be the same chat.")
        warn = ""
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            if getattr(member, "status", "") not in ("administrator", "owner"):
                warn = "\n⚠️ I don't look like admin there — add me as admin or reading will fail."
        except Exception:
            warn = "\n⚠️ Couldn't verify my admin status — make sure I'm admin there."
        db.set("src2", chat.id)
        q = db.q2_count(chat.id)
        await _reply(
            message,
            f"✅ Source B: <b>{chat.title or chat.id}</b> (<code>{chat.id}</code>){warn}\n"
            f"Queued: {q} · /source2 on to start, /backfill2 for older posts.",
        )

    @bot.on_message(filters.private & filters.command("setdest2") & owner_only)
    async def _setdest2(_, message):
        args = _args(message)
        if not args:
            return await _reply(
                message,
                _usage("/setdest2 <id | @username | main>", "'main' uses the normal destination."),
            )
        if args[0].lower() == "main":
            db.set("dest2", None)
            return await _reply(message, "✅ Source B destination: <b>main</b>.")
        chat = await _resolve(bot, parse_target(args[0]), message, "destination")
        if not chat:
            return
        if str(chat.id) == str(db.get("src2")):
            return await _reply(message, "Source and destination can't be the same chat.")
        db.set("dest2", chat.id)
        await _reply(message, f"✅ Source B destination: <b>{chat.title or chat.id}</b>.")

    @bot.on_message(filters.private & filters.command("source2") & owner_only)
    async def _source2(_, message):
        args = _args(message)
        v = parse_on_off(args[0]) if args else None
        if v is None:
            snap = eng2.snapshot() if eng2 else {}
            return await _reply(
                message,
                _usage(
                    "/source2 on|off",
                    f"Now: <b>{'on' if db.get('on2') else 'off'}</b> · queued: {snap.get('queue', 0)}",
                ),
            )
        if v and not db.get("src2"):
            return await _reply(message, "Set Source B first (/setsource2).")
        db.set("on2", v)
        if v and eng2 and not eng2.is_running:
            eng2.start()
        q = db.q2_count(db.get("src2")) if db.get("src2") else 0
        await _reply(
            message,
            f"✅ Source B <b>{'started' if v else 'stopped'}</b>. Queued: {q}." + ("" if v else " Queue is kept."),
        )

    @bot.on_message(filters.private & filters.command("backfill2") & owner_only)
    async def _backfill2(_, message):
        args = _args(message)
        if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
            return await _reply(
                message,
                _usage("/backfill2 <from_id> <to_id>", "Queue an older id range (max 2,000,000 ids)."),
            )
        start, end = int(args[0]), int(args[1])
        if end < start:
            start, end = end, start
        if not db.get("src2"):
            return await _reply(message, "Set Source B first (/setsource2).")
        if end - start > 2_000_000:
            return await _reply(message, "Range too big — max 2,000,000 ids.")
        n = db.q2_add_range(db.get("src2"), start, end)
        await _reply(
            message,
            f"✅ Queued <b>{end - start + 1}</b> ids ({n} new). "
            f"Total pending: {db.q2_count(db.get('src2'))}.",
        )

    @bot.on_message(filters.private & filters.command("queue2") & owner_only)
    async def _queue2(_, message):
        args = _args(message)
        src2 = db.get("src2")
        if args and args[0].lower() == "clear":
            if src2:
                db.q2_clear(src2)
            return await _reply(message, "🗑 Source B queue cleared.")
        snap = eng2.snapshot() if eng2 else {}
        await _reply(
            message,
            f"<b>Source B queue</b>\n"
            f"Pending: <b>{snap.get('queue', 0)}</b> · state: {snap.get('state', 'idle')}\n"
            f"Copied: {snap.get('sent', 0)} · skipped: {snap.get('skipped', 0)} · failed: {snap.get('failed', 0)}\n"
            f"<code>/queue2 clear</code> to empty it.",
        )

    # ---------------- live capture for Source B ----------------
    @bot.on_message(filters.channel | filters.group)
    async def _live_capture(_, message):
        try:
            src2 = db.get("src2")
            if not src2 or not db.get("on2") or not db.get("live2"):
                return
            chat = message.chat
            if chat is None or chat.id != src2:
                return
            if getattr(message, "empty", False) or getattr(message, "service", False):
                return
            if getattr(message, "outgoing", False):
                return
            db.q2_add(src2, message.id)
        except Exception:
            log.warning("live capture failed", exc_info=True)

    # ---------------- status panel ----------------
    panel = StatusPanel(bot, ctx)
    ctx.panel = panel

    @bot.on_message(filters.private & filters.command("status") & owner_only)
    async def _status(_, message):
        await panel.show(message)

    @bot.on_callback_query(filters.regex(r"^st:"))
    async def _status_btn(_, query):
        if query.from_user is None or query.from_user.id != OWNER:
            return  # silent for everyone else
        try:
            await panel.on_button(query)
        except Exception:
            log.warning("status button failed", exc_info=True)


# ======================================================================
# Live /status panel
# ======================================================================

PANEL_TTL = 15 * 60  # stop auto-refresh after 15 min idle
PANEL_INTERVAL = 5


def _bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "█" * filled + "░" * (width - filled)


def _dur(secs: float) -> str:
    s = int(max(0, secs))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class StatusPanel:
    def __init__(self, bot, ctx):
        self.bot = bot
        self.ctx = ctx
        self.chat_id = None
        self.msg_id = None
        self.task = None
        self.last_tap = 0.0

    def _snap(self):
        return self.ctx.eng_a.snapshot() if self.ctx.eng_a else {}

    def _snap2(self):
        return self.ctx.eng_b.snapshot() if self.ctx.eng_b else {}

    def _text(self) -> str:
        db = self.ctx.db
        s = self._snap()
        s2 = self._snap2()

        has_session = self.ctx.cfg.has_session
        running = db.get("running")
        paused = db.get("paused")
        a_primary = bool(has_session and (running or paused))
        b_primary = bool(not a_primary and s2.get("configured"))

        if a_primary:
            if paused:
                icon, state = "\U0001F7E1", "paused"
            elif running:
                icon = "\U0001F7E2"
                st = s.get("state", "idle")
                if st == "copying":
                    state = "copying"
                elif st in ("gap", "cooldown"):
                    pacer = getattr(self.ctx.eng_a, "_pacer", None)
                    left = pacer.remaining() if pacer is not None else 0.0
                    state = f"{'cooldown' if st == 'cooldown' else 'gap'} ({_dur(left)} left)"
                elif st == "flood-wait":
                    state = f"flood-wait ({_dur(s.get('state_secs', 0))})"
                elif st == "watching":
                    state = "watching for new files"
                else:
                    state = st
            else:
                icon, state = "\U000026AA", "idle"

            total = s.get("total", 0) or 0
            pos = s.get("position", 0) or 0
            latest = getattr(self.ctx.eng_a, "_latest", 0) if self.ctx.eng_a else 0
            start_last = latest - total
            if total > 0:
                done_share = max(0.0, min(100.0, (pos - start_last) / total * 100))
            else:
                done_share = 0.0

            lines = [
                f"{icon} <b>Copy status</b> — Source A {state}",
                f"<code>{_bar(done_share)}</code> {done_share:.0f}%",
                f"Position: msg <code>{pos}</code> / <code>{latest or '—'}</code>",
                f"Copied: <b>{s.get('sent', 0)}</b> · skipped: {s.get('skipped', 0)} · failed: {s.get('failed', 0)}",
                f"Batch: {s.get('batch_n', 0)}/{s.get('batch', 20)} "
                f"(gap {s.get('delay', 3)}s · wait {s.get('wait', 30)}s)",
                f"Speed: {s.get('speed', 0):.1f}/min · ETA: {_dur(s.get('eta', 0))} · elapsed: {_dur(s.get('elapsed', 0))}",
            ]
        elif b_primary:
            on = bool(s2.get("on"))
            st = s2.get("state", "idle")
            icon = "\U0001F7E2" if on else "\U000026AA"
            state = f"Source B {st}" if on else "Source B idle"
            sent = s2.get("sent", 0) or 0
            skipped = s2.get("skipped", 0) or 0
            failed = s2.get("failed", 0) or 0
            queue = s2.get("queue", 0) or 0
            done = sent + skipped + failed
            total = done + queue
            done_share = (done / total * 100) if total > 0 else 0.0
            eng_b = self.ctx.eng_b
            t0 = getattr(eng_b, "started_at", None) if eng_b else None
            elapsed = (time.time() - t0) if t0 else 0.0
            speed = (sent / elapsed * 60) if elapsed > 0 else 0.0
            eta = (queue / (sent / elapsed)) if elapsed > 0 and sent > 0 else 0.0
            lines = [
                f"{icon} <b>Copy status</b> — {state}",
                f"<code>{_bar(done_share)}</code> {done_share:.0f}%",
                f"Processed: <code>{done}</code> · queue: <code>{queue}</code>",
                f"Copied: <b>{sent}</b> · skipped: {skipped} · failed: {failed}",
                f"Batch: {s2.get('batch_n', 0)}/{db.get('batch', 20)} "
                f"(gap {db.get('delay', 3)}s · wait {db.get('wait', 30)}s)",
                f"Speed: {speed:.1f}/min · ETA: {_dur(eta)} · elapsed: {_dur(elapsed)}",
            ]
        else:
            if not has_session:
                state = "idle — no SESSION_STRING (Source A disabled)"
            else:
                state = "idle"
            lines = [
                f"\U000026AA <b>Copy status</b> — {state}",
                f"<code>{_bar(0.0)}</code> 0%",
            ]

        lines.append(
            f"All-time: {db.stat_get('total_sent')} sent · {db.stat_get('total_failed')} failed"
        )
        if b_primary:
            pass  # already the main section
        elif s2.get("configured"):
            st2 = "\U0001F7E2" if s2.get("on") else "\U000026AA"
            lines.append(
                f"Source B: {st2} {s2.get('state', 'idle')} · queue {s2.get('queue', 0)} · "
                f"copied {s2.get('sent', 0)}"
            )
        lines.append(f"<i>Updated {utc_now_str()} UTC</i>")
        return "\n".join(lines)

    def _markup(self):
        paused = self.ctx.db.get("paused")
        toggle = InlineKeyboardButton(
            "▶️ Resume" if paused else "⏸ Pause", callback_data="st:pause"
        )
        return InlineKeyboardMarkup(
            [
                [toggle, InlineKeyboardButton("⏹ Stop", callback_data="st:stop")],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="st:refresh"),
                    InlineKeyboardButton("❌ Close", callback_data="st:close"),
                ],
            ]
        )

    async def show(self, message):
        # a new /status deletes the old one
        old_chat = self.ctx.db.get("panel_chat")
        old_msg = self.ctx.db.get("panel_msg")
        if old_chat and old_msg:
            try:
                await self.bot.delete_messages(old_chat, old_msg)
            except Exception:
                pass
        sent = await message.reply_text(
            self._text(), parse_mode=ParseMode.HTML, reply_markup=self._markup()
        )
        self.chat_id = sent.chat.id
        self.msg_id = sent.id
        self.ctx.db.set("panel_chat", self.chat_id)
        self.ctx.db.set("panel_msg", self.msg_id)
        self.last_tap = time.time()
        self._restart_loop()

    def _restart_loop(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            while time.time() - self.last_tap < PANEL_TTL:
                await asyncio.sleep(PANEL_INTERVAL)
                if not self.chat_id or not self.msg_id:
                    return
                try:
                    await self.bot.edit_message_text(
                        self.chat_id,
                        self.msg_id,
                        self._text(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._markup(),
                    )
                except Exception as e:
                    name = type(e).__name__
                    if name == "MessageNotModified":
                        continue
                    if name == "FloodWait":
                        await asyncio.sleep((getattr(e, "value", 0) or 0) + 1)
                        continue
                    log.warning("panel edit failed: %r", e)
                    return  # message deleted or gone -> stop
        except asyncio.CancelledError:
            pass

    async def on_button(self, query):
        # adopt the panel after a restart
        msg = query.message
        if msg:
            self.chat_id = msg.chat.id
            self.msg_id = msg.id
            self.ctx.db.set("panel_chat", self.chat_id)
            self.ctx.db.set("panel_msg", self.msg_id)
        self.last_tap = time.time()
        data = query.data or ""
        db = self.ctx.db
        eng = self.ctx.eng_a

        if data == "st:pause":
            db.set("paused", not db.get("paused"))
            self._restart_loop()
        elif data == "st:stop":
            db.set("running", False)
            db.set("paused", False)
            if eng:
                await eng.stop()
        elif data == "st:refresh":
            self._restart_loop()
        elif data == "st:close":
            if self.task and not self.task.done():
                self.task.cancel()
            try:
                await query.message.delete()
            except Exception:
                pass
            self.chat_id = self.msg_id = None
            db.set("panel_chat", None)
            db.set("panel_msg", None)
            try:
                await query.answer()
            except Exception:
                pass
            return

        try:
            await self.bot.edit_message_text(
                self.chat_id,
                self.msg_id,
                self._text(),
                parse_mode=ParseMode.HTML,
                reply_markup=self._markup(),
            )
        except Exception as e:
            if type(e).__name__ not in ("MessageNotModified",):
                log.warning("panel button edit failed: %r", e)
        try:
            await query.answer()
        except Exception:
            pass
