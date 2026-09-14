"""Application configuration loaded from environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # Telegram (Telethon, user's own account via my.telegram.org)
    telegram_api_id: int = _env_int("TELEGRAM_API_ID", 0)
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    telegram_phone: str = os.getenv("TELEGRAM_PHONE", "")
    telegram_session_path: str = str(DATA_DIR / "telegram.session")

    # Telegram Bot API (@CariOra_bot) — data-entry / category-lookup bot,
    # separate credential from the Telethon user account above
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # Anthropic (stage 2 classification/extraction)
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    # Google Sheets
    google_service_account_file: str = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE", str(DATA_DIR / "google_service_account.json")
    )
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_worksheet_name: str = os.getenv("GOOGLE_WORKSHEET_NAME", "Карточки")

    # Business rules (from TZ)
    cards_per_run: int = _env_int("CARDS_PER_RUN", 50)
    depth_months: int = _env_int("DEPTH_MONTHS", 6)

    # Storage
    db_path: str = str(DATA_DIR / "app.db")


settings = Settings()
