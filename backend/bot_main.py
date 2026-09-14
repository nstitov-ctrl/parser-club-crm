"""Runs @CariOra_bot (Telegram Bot API, aiogram) — separate process from the
FastAPI CRM server:

    cd backend
    source .venv/bin/activate
    python bot_main.py

Requires TELEGRAM_BOT_TOKEN in backend/.env (get it from @BotFather).
"""
from __future__ import annotations

import asyncio
import logging

from app import bot


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
