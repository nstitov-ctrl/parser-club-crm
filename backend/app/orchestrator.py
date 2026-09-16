"""Implements the single-pass run logic from TZ §3: collect exactly
CARDS_PER_RUN (50) cards, walking chats in order, resuming from the saved
position, respecting the 6-month depth cutoff, moving to the next chat when
one is exhausted, and persisting position after every message so a run can
be safely resumed on the next "Start" click.

All blocking calls (sqlite, the Anthropic HTTP call, the Google Sheets HTTP
call) run via asyncio.to_thread so a long run never stalls the event loop —
otherwise Telethon's own network I/O and the /api/status polling endpoint
would freeze for the whole duration of a run.

Between channels, a random delay is inserted before touching Telegram
(ban-risk mitigation, user-requested): hammering many different channels
back-to-back is the pattern that stands out, not reading messages within
one channel — so the pause sits at channel switches only, not per message.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import database, filtering, sheets_writer, telegram_source
from app.config import settings

_current_task: Optional[asyncio.Task] = None

# Random delay before starting a new channel, seconds (not applied before the
# very first channel of a run — no prior Telegram activity to space out yet).
CHAT_SWITCH_DELAY_RANGE = (2, 60)


def is_running() -> bool:
    return _current_task is not None and not _current_task.done()


def start_run_background() -> None:
    global _current_task
    if is_running():
        raise RuntimeError("Проход уже выполняется")
    _current_task = asyncio.create_task(_run_pass())


def _dedup_hash(username: Optional[str], text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    raw = f"{(username or '').lower()}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _run_pass() -> None:
    run_id = await asyncio.to_thread(database.create_run)
    cards_collected = 0
    messages_viewed = 0
    # Approximate month->days depth cutoff (TZ §2: "не старше 6 месяцев");
    # exact calendar precision isn't required, per-day granularity is enough.
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.depth_months * 30)
    is_first_chat = True

    try:
        while cards_collected < settings.cards_per_run:
            chat = await asyncio.to_thread(database.get_next_active_chat)
            if chat is None:
                break  # no more chats to process (TZ §3.5)

            if not is_first_chat:
                await asyncio.sleep(random.uniform(*CHAT_SWITCH_DELAY_RANGE))
            is_first_chat = False

            if chat["status"] == "not_started":
                try:
                    title = await telegram_source.resolve_chat_title(chat["identifier"])
                except Exception:
                    title = chat["identifier"]
                await asyncio.to_thread(database.mark_chat_in_progress, chat["id"], title)

            chat_exhausted = False
            # last_message_id is the OLDEST message processed so far while
            # walking backward from "now"; resuming continues further back
            # from there (see telegram_source.iter_new_messages).
            before_id = chat["last_message_id"]

            async for message in telegram_source.iter_new_messages(chat["identifier"], before_id):
                if message.date and message.date < cutoff:
                    chat_exhausted = True
                    break

                # raw attribute (not the markdown-rendered .text) to keep the
                # original post exactly as authored, per TZ §5.
                text = message.message or ""
                card_added = False

                if text and filtering.stage1_passes(text):
                    username, prof_first, prof_last = await telegram_source.get_author(message)
                    extracted = await asyncio.to_thread(
                        filtering.classify_and_extract, text, prof_first, prof_last
                    )
                    if extracted and extracted["is_service_ad"]:
                        published_at = message.date.isoformat() if message.date else ""
                        dedup_hash = _dedup_hash(username, text)
                        final_first = extracted["first_name"] or prof_first
                        final_last = extracted["last_name"] or prof_last
                        category = sheets_writer.normalize_category(extracted["category"])
                        # Catches reposts of the same offer reworded (different
                        # text defeats dedup_hash above, but same author +
                        # same category is a reliable "already have this" signal).
                        already_have = await asyncio.to_thread(
                            database.author_category_exists, username, category
                        )
                        inserted = False
                        if not already_have:
                            inserted = await asyncio.to_thread(
                                database.insert_card,
                                chat_id=chat["id"],
                                message_id=message.id,
                                nickname=username,
                                first_name=final_first,
                                last_name=final_last,
                                category=category,
                                original_text=text,
                                extra_contacts=extracted["extra_contacts"],
                                published_at=published_at,
                                dedup_hash=dedup_hash,
                            )
                        if inserted:
                            await asyncio.to_thread(
                                sheets_writer.append_card,
                                nickname=username,
                                first_name=final_first,
                                last_name=final_last,
                                category=category,
                                original_text=text,
                                extra_contacts=extracted["extra_contacts"],
                                published_at=published_at,
                            )
                            card_added = True
                            cards_collected += 1

                messages_viewed += 1
                await asyncio.to_thread(
                    database.update_chat_position,
                    chat_id=chat["id"],
                    last_message_id=message.id,
                    last_message_date=message.date.isoformat() if message.date else "",
                    messages_viewed_delta=1,
                    cards_collected_delta=1 if card_added else 0,
                )
                await asyncio.to_thread(
                    database.update_run_progress, run_id, cards_collected, messages_viewed
                )

                if cards_collected >= settings.cards_per_run:
                    break
            else:
                # generator exhausted naturally: walked all the way back to
                # the very first message ever posted in the chat — its whole
                # history is younger than the depth cutoff
                chat_exhausted = True

            if chat_exhausted:
                await asyncio.to_thread(database.mark_chat_done, chat["id"])

        await asyncio.to_thread(database.finish_run, run_id, status="done")
    except Exception as exc:  # keep partial progress, surface the error
        await asyncio.to_thread(database.finish_run, run_id, status="error", error=str(exc))
        raise
