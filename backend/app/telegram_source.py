"""Read-only Telegram access via Telethon, using the user's own account and
the official MTProto API (api_id/api_hash from my.telegram.org).

Only reads public channel/group history (get_entity + iter_messages) —
no joining, no posting, no spam actions. Reading public channels by
username does not require membership.
"""
from __future__ import annotations

from typing import AsyncIterator, Optional, Tuple

from telethon import TelegramClient
from telethon.tl.types import Message

from app.config import settings

_client: Optional[TelegramClient] = None


def get_client() -> TelegramClient:
    global _client
    if _client is None:
        _client = TelegramClient(
            settings.telegram_session_path,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
    return _client


async def ensure_connected() -> TelegramClient:
    client = get_client()
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram-сессия не авторизована. Запустите один раз из backend/: "
            "python login_telegram.py"
        )
    return client


async def disconnect() -> None:
    client = get_client()
    if client.is_connected():
        await client.disconnect()


async def resolve_chat_title(identifier: str) -> str:
    client = await ensure_connected()
    entity = await client.get_entity(identifier)
    return getattr(entity, "title", None) or getattr(entity, "username", None) or identifier


async def iter_new_messages(
    identifier: str, before_message_id: Optional[int]
) -> AsyncIterator[Message]:
    """Yields messages newest->oldest (Telegram's natural default order),
    starting strictly before before_message_id (or from the very latest
    message in the channel when None).

    This walks backward from "now", matching TZ §3: read messages until one
    older than the depth cutoff is hit, at which point the whole earlier
    history can be skipped — a channel active for years would otherwise
    force reading years of history just to reach the last 6 months if we
    walked oldest->newest instead."""
    client = await ensure_connected()
    entity = await client.get_entity(identifier)
    offset_id = before_message_id or 0
    async for message in client.iter_messages(entity, offset_id=offset_id):
        yield message


async def get_author(message: Message) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (username, first_name, last_name) of the message sender.
    Falls back to the channel's own username if the message has no user
    sender (e.g. posted as the channel)."""
    sender = await message.get_sender()
    if sender is None:
        return None, None, None
    username = getattr(sender, "username", None)
    first_name = getattr(sender, "first_name", None)
    last_name = getattr(sender, "last_name", None)
    return username, first_name, last_name
