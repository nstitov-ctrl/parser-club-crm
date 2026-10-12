"""One-off / resumable: fill the "ID автора" column for cards collected before
the scraper started saving it.

The self-vote check works best on the numeric Telegram ID (a username can be
changed or hidden). For old cards we only have the @nick, so resolve it via
the user's own Telethon session — slowly, because Telegram rate-limits
username lookups (FloodWait). Safe to stop and re-run: rows that already have
an ID are skipped, and writes are batched so progress is never lost.

Run from backend/ while no scrape is running:   python -m app.backfill_author_ids
"""
from __future__ import annotations

import asyncio
import logging
import random

import gspread
from telethon import errors
from telethon.tl.types import User

from app import sheets_writer, telegram_source

logger = logging.getLogger(__name__)

PAUSE_RANGE = (1.5, 3.0)     # seconds between lookups
MAX_FLOOD_WAIT = 120         # longer than this -> stop and resume another time
BATCH = 20                   # nicks per sheet write


async def main() -> None:
    ws = sheets_writer._get_worksheet()
    header = ws.row_values(1)
    id_col = header.index("ID автора") + 1
    cards = sheets_writer.list_cards_with_rows()

    todo: dict[str, list[int]] = {}
    for card in cards:
        nick = str(card.get("Ник", "")).strip().lstrip("@")
        if nick and not str(card.get("ID автора", "")).strip():
            todo.setdefault(nick.lower(), []).append(card["_row"])
    print(f"{len(todo)} unique nicks without an ID ({sum(len(v) for v in todo.values())} rows)", flush=True)

    client = await telegram_source.ensure_connected()
    pending: list[gspread.Cell] = []
    resolved = unresolved = 0

    def flush() -> None:
        nonlocal pending
        if pending:
            ws.update_cells(pending, value_input_option="RAW")
            pending = []

    try:
        for n, (nick, rows) in enumerate(todo.items(), start=1):
            try:
                entity = await client.get_entity(nick)
            except errors.FloodWaitError as exc:
                if exc.seconds > MAX_FLOOD_WAIT:
                    print(f"FloodWait {exc.seconds}s — stopping; re-run later to continue", flush=True)
                    break
                await asyncio.sleep(exc.seconds + 2)
                continue
            except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                unresolved += 1                       # deleted / renamed account
            except Exception as exc:                  # never lose progress to one odd nick
                logger.warning("lookup failed for %s: %s", nick, exc)
                unresolved += 1
            else:
                if isinstance(entity, User):          # a channel/chat handle isn't a person
                    pending += [gspread.Cell(r, id_col, entity.id) for r in rows]
                    resolved += 1
                else:
                    unresolved += 1
            if len(pending) >= BATCH:
                flush()
            if n % 25 == 0:
                print(f"  {n}/{len(todo)} looked up — resolved {resolved}, unresolved {unresolved}", flush=True)
            await asyncio.sleep(random.uniform(*PAUSE_RANGE))
    finally:
        flush()
        await telegram_source.disconnect()
    print(f"done: resolved {resolved}, unresolved {unresolved} (those keep username-based self-vote check)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
