# Telegram Channel Copy Bot

Copies files (not forwards — no "Forwarded from" tag) from a source channel/group
into your destination channel, at a controlled pace: **3s gap between files,
30s cooldown after every 20 files** (all configurable from the bot).

Two independent sources:

- **Source A** — channels/groups *you* can read but the bot can't. Reads through
  your own account via a session string.
- **Source B** — channels where **the bot itself is admin**. No session string
  needed; the bot reads and copies directly.

## Quick start

```bash
cp .env.example .env   # fill in your values
docker compose up -d --build
```

Then open a private chat with your bot and press /start.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) → `BOT_TOKEN`.
2. Get `API_ID` / `API_HASH` from https://my.telegram.org.
3. Get your numeric user id (e.g. [@userinfobot](https://t.me/userinfobot)) → `OWNER_ID`.
4. **Source A only:** run `python gen_session.py`, log in with your Telegram
   account, paste the printed string as `SESSION_STRING`.

### Source B-only quickstart (no session string)

Leave `SESSION_STRING` empty. The bot starts normally; Source A commands will
tell you a session string is needed, everything else works.

1. Add your bot as **admin** to the source channel (needs *Post Messages* in the
   destination; reading needs admin in the source).
2. `/setsource2 @channelusername` (or numeric id). If the bot can't see the
   chat, forward any post from that channel to the bot in private chat.
3. `/setdest2 main` (or a channel id) — `main` reuses the normal destination.
4. `/backfill2 1 5000` to queue older posts, or just `/source2 on` — new posts
   are captured live and copied automatically.

## Running

**Docker (VPS):** `docker compose up -d --build` · logs: `docker compose logs -f`

**Local:**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in
python main.py
```

**Koyeb / Render:** deploy as a Web Service, set the env vars, and set the
health check path to `/`. ⚠️ **Ephemeral disks:** Koyeb/Render wipe the disk on
redeploy — progress and the duplicate memory live in SQLite, so **attach a
persistent volume** and point `DB_PATH` at it (e.g. `DB_PATH=/data/bot.db`),
otherwise progress resets on every deploy. The bot session file also lives in
`data/`, so mount the volume there too.

## Commands

**Setup:** `/setsource`, `/setdest`, `/setskip`, `/setend`
**Run:** `/startcopy` (`/copy`), `/pause`, `/resume`, `/stop`, `/status`, `/live on|off`, `/resetprogress`
**Pacing:** `/setdelay`, `/setbatch`, `/setwait`
**Caption:** `/setcaption`, `/setprefix`, `/setsuffix`, `/nocaption`, `/removeat`,
`/removelinks`, `/addblock`, `/delblock`, `/blocklist`, `/replace`, `/delreplace`, `/replacelist`
**Filters:** `/types`, `/dedupe`
**Info:** `/settings`, `/resetsettings`
**Source B:** `/setsource2`, `/setdest2`, `/source2 on|off`, `/backfill2`, `/queue2`

Caption templates support `{caption}`, `{filename}`, `{size}`, `{id}` and HTML.

## Notes

- **Owner-only:** every command and button works only for `OWNER_ID` in a
  private chat. Everyone else is silently ignored.
- **Protected content refused:** sources with "Restrict saving content" enabled
  are refused — at set-source time and again when copying starts. There is no
  workaround, by design.
- **Only copy content you have the right to redistribute.** You are responsible
  for what the bot copies.
- ⚠️ **Session string = full access to your Telegram account.** Never share it,
  never commit `.env`, never paste it in chat. It lives only in your `.env`.
- Progress is saved after every message; after a restart the bot auto-resumes.
  `/stop` clears the running flag, `/resetprogress` clears progress + dup memory.
