"""Configuration loaded from environment (.env)."""

import os
from dataclasses import dataclass


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    session_string: str  # optional: only needed for Source A
    port: int
    db_path: str

    @property
    def has_session(self) -> bool:
        return bool(self.session_string)


def load_config() -> Config:
    from dotenv import load_dotenv

    load_dotenv()
    return Config(
        api_id=_int("API_ID", 0),
        api_hash=os.getenv("API_HASH", ""),
        bot_token=os.getenv("BOT_TOKEN", ""),
        owner_id=_int("OWNER_ID", 0),
        session_string=os.getenv("SESSION_STRING", "").strip(),
        port=_int("PORT", 8000),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
    )
