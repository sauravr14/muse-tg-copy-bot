"""SQLite persistence (WAL mode). Tables: settings, progress, files, stats, q2."""

import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS progress(src TEXT PRIMARY KEY, last_id INTEGER);
CREATE TABLE IF NOT EXISTS files(file_unique_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS stats(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS q2(src TEXT, id INTEGER, PRIMARY KEY(src, id));
"""

DEFAULTS = {
    "delay": 3,
    "batch": 20,
    "wait": 30,
    "types": ["document", "video", "audio", "photo", "animation", "voice"],
    "dedupe": True,
    "remove_usernames": False,
    "remove_links": False,
    "nocaption": False,
    "live": False,
    "template": "",
    "prefix": "",
    "suffix": "",
    "blocklist": [],
    "replacements": {},
    "src": None,
    "dest": None,
    "end_id": 0,
    "src2": None,
    "dest2": None,
    "on2": False,
    "live2": True,
    "running": False,
    "paused": False,
    "panel_chat": None,
    "panel_msg": None,
}


class DB:
    def __init__(self, path: str):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _exec(self, sql, args=(), fetch=None):
        with self._lock:
            cur = self.conn.execute(sql, args)
            if fetch == "one":
                row = cur.fetchone()
                self.conn.commit()
                return row
            if fetch == "all":
                rows = cur.fetchall()
                self.conn.commit()
                return rows
            self.conn.commit()
            return cur.rowcount

    # ---- settings ----
    def get(self, key, default=None):
        if default is None:
            default = DEFAULTS.get(key)
        row = self._exec("SELECT v FROM settings WHERE k=?", (key,), "one")
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return default

    def set(self, key, value):
        self._exec(
            "INSERT INTO settings(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )

    def reset_settings(self):
        for key, val in DEFAULTS.items():
            if key in ("running", "paused"):
                continue
            self.set(key, val)

    # ---- progress (per source, last processed message id) ----
    def progress_get(self, src):
        row = self._exec(
            "SELECT last_id FROM progress WHERE src=?", (str(src),), "one"
        )
        return row[0] if row else 0

    def progress_set(self, src, last_id):
        self._exec(
            "INSERT INTO progress(src, last_id) VALUES(?, ?) "
            "ON CONFLICT(src) DO UPDATE SET last_id=excluded.last_id",
            (str(src), int(last_id)),
        )

    def progress_clear(self, src):
        self._exec("DELETE FROM progress WHERE src=?", (str(src),))

    # ---- duplicate memory (shared by both sources) ----
    def file_seen(self, file_unique_id) -> bool:
        row = self._exec(
            "SELECT 1 FROM files WHERE file_unique_id=?", (file_unique_id,), "one"
        )
        return row is not None

    def file_add(self, file_unique_id):
        self._exec(
            "INSERT OR IGNORE INTO files(file_unique_id) VALUES(?)",
            (file_unique_id,),
        )

    def files_clear(self):
        self._exec("DELETE FROM files")

    # ---- stats ----
    def stat_get(self, key, default=0):
        row = self._exec("SELECT v FROM stats WHERE k=?", (key,), "one")
        if row is None:
            return default
        try:
            return int(row[0])
        except (ValueError, TypeError):
            return default

    def stat_set(self, key, value):
        self._exec(
            "INSERT INTO stats(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(int(value))),
        )

    def stat_incr(self, key, n=1):
        self._exec(
            "INSERT INTO stats(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+?",
            (key, str(n), n),
        )

    # ---- Source B queue ----
    def q2_add(self, src, msg_id) -> bool:
        n = self._exec(
            "INSERT OR IGNORE INTO q2(src, id) VALUES(?, ?)",
            (str(src), int(msg_id)),
        )
        return n == 1

    def q2_add_range(self, src, start, end) -> int:
        if end < start:
            return 0
        rows = [(str(src), i) for i in range(int(start), int(end) + 1)]
        before = self.q2_count(src)
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO q2(src, id) VALUES(?, ?)", rows
            )
            self.conn.commit()
        return self.q2_count(src) - before

    def q2_next(self, src, limit=100):
        rows = self._exec(
            "SELECT id FROM q2 WHERE src=? ORDER BY id ASC LIMIT ?",
            (str(src), int(limit)),
            "all",
        )
        return [r[0] for r in rows]

    def q2_drop(self, src, msg_id):
        self._exec("DELETE FROM q2 WHERE src=? AND id=?", (str(src), int(msg_id)))

    def q2_clear(self, src):
        self._exec("DELETE FROM q2 WHERE src=?", (str(src),))

    def q2_count(self, src) -> int:
        row = self._exec(
            "SELECT COUNT(*) FROM q2 WHERE src=?", (str(src),), "one"
        )
        return row[0] if row else 0

    def close(self):
        with self._lock:
            self.conn.commit()
            self.conn.close()
