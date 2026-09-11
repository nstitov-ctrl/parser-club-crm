"""One-time interactive Telegram login (run manually, once):

    cd backend
    source .venv/bin/activate
    python login_telegram.py

Creates the local session file (backend/data/telegram.session) using your
own account via the official MTProto API (api_id/api_hash from
my.telegram.org, set in .env). Telethon will prompt for your phone number
(if not already in .env) and the login code Telegram sends you. After this,
the FastAPI app reuses the saved session non-interactively.
"""
from __future__ import annotations

from app.config import settings
from app.telegram_source import get_client


def main() -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise SystemExit(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в backend/.env "
            "(получить на https://my.telegram.org)"
        )
    client = get_client()
    with client:
        client.loop.run_until_complete(
            client.start(phone=lambda: settings.telegram_phone or input("Телефон (+код страны): "))
        )
        me = client.loop.run_until_complete(client.get_me())
        print(f"Готово. Авторизован как: {me.first_name} (@{me.username})")


if __name__ == "__main__":
    main()
