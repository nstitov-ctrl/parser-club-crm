# GENERATED FILE — do not edit. Source: backend/app/authoring.py
# Refresh with: python miniapp/sync_votes.py
"""What an author may do with their OWN cards — shared by the Telegram bot and
the mini app, so both follow the same rules.

An author is whoever writes from the @username in the card's "Ник" column
(verification is by nick only). They can confirm a card, correct its name /
category / text, delete it, or create a new one:

  * confirming or correcting stamps today's date in "Подтверждено" (the check
    mark lasts ~6 months, see votes.is_verified);
  * a corrected text lives in "Текст от автора" and is shown INSTEAD of the
    scraped original, which stays untouched;
  * deleting is soft: "Удалить?" gets "УДАЛЕНО автором <date>", the card
    disappears from search/cabinet, the row stays in the sheet (recoverable;
    removing rows for good remains a manual job);
  * one author + one category = one card (the project's dedup rule).

Worksheet access is passed in (no imports of config/credentials), so this file
also runs inside the Vercel mini app, where miniapp/sync_votes.py copies it as
api/_authoring.py.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

import gspread

import _votes as votes

EDITABLE = {"Имя", "Направление", votes.AUTHOR_TEXT_COL}
MAX_TEXT_LEN = 1000
MAX_NAME_LEN = 120
MAX_CATEGORY_LEN = 80


class CardUpdateError(Exception):
    """Refusal with a message that is safe to show to the user."""


def _nick(value: object) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _today() -> str:
    return dt.date.today().isoformat()


def visible(cards: list[dict]) -> list[dict]:
    """Cards the author hasn't deleted."""
    return [c for c in cards if not votes.is_deleted(c)]


def find_my_cards(cards: list[dict], username: Optional[str]) -> list[dict]:
    """Visible cards whose "Ник" equals the caller's @username (case-insensitive).
    A @handle that only appears inside "Дополнительные контакты" is NOT enough."""
    me = _nick(username)
    if not me:
        return []
    return [c for c in visible(cards) if _nick(c.get("Ник")) == me]


def _own_card(cards: list[dict], card_id: str, username: Optional[str]) -> dict:
    card = next((c for c in visible(cards) if votes.card_id(c) == card_id), None)
    if card is None:
        raise CardUpdateError("Карточка изменилась или удалена — обнови список.")
    if not _nick(username) or _nick(card.get("Ник")) != _nick(username):
        raise CardUpdateError("Эта карточка привязана к другому нику.")
    return card


def _clean(field: str, value: str) -> str:
    value = " ".join(str(value or "").split()) if field != votes.AUTHOR_TEXT_COL else str(value or "").strip()
    limits = {"Имя": MAX_NAME_LEN, "Направление": MAX_CATEGORY_LEN, votes.AUTHOR_TEXT_COL: MAX_TEXT_LEN}
    if not value:
        raise CardUpdateError("Поле не может быть пустым.")
    if len(value) > limits[field]:
        raise CardUpdateError(f"Слишком длинно (максимум {limits[field]} символов).")
    return value


def update_card(
    ws: gspread.Worksheet,
    ledger: votes.Ledger,
    cards: list[dict],
    card_id: str,
    username: Optional[str],
    changes: dict[str, str],
) -> dict:
    """Confirms (empty `changes`) or corrects one of the caller's cards and stamps
    today's date. `cards` must be a fresh read with "_row", so a stale row number
    can never hit the wrong card."""
    if set(changes) - EDITABLE:
        raise CardUpdateError("Это поле менять нельзя.")
    card = _own_card(cards, card_id, username)
    changes = {k: _clean(k, v) for k, v in changes.items()}

    old_key = votes.card_key(card)
    updated = dict(card, **changes)
    new_key = votes.card_key(updated)
    if new_key != old_key and any(votes.card_key(c) == new_key for c in visible(cards) if c["_row"] != card["_row"]):
        raise CardUpdateError("У тебя уже есть объявление в этой категории.")

    today = _today()
    header = ws.row_values(1)
    cells = [gspread.Cell(card["_row"], header.index(k) + 1, v) for k, v in changes.items()]
    cells.append(gspread.Cell(card["_row"], header.index(votes.CONFIRMED_COL) + 1, today))
    ws.update_cells(cells, value_input_option="RAW")
    if new_key != old_key:
        ledger.migrate_key(old_key, new_key)          # votes/contacts keep following the card
    updated[votes.CONFIRMED_COL] = today
    return updated


def delete_card(
    ws: gspread.Worksheet, cards: list[dict], card_id: str, username: Optional[str]
) -> None:
    """Soft delete: marks the row, nothing is removed from the sheet."""
    card = _own_card(cards, card_id, username)
    header = ws.row_values(1)
    ws.update_cells(
        [gspread.Cell(card["_row"], header.index(votes.DELETED_COL) + 1,
                      f"{votes.DELETED_PREFIX} автором {_today()}")],
        value_input_option="RAW",
    )


def duplicate_exists(cards: list[dict], username: Optional[str], category: str) -> bool:
    """Does this author already have a (visible) card in this category?"""
    probe = {"Ник": f"@{_nick(username)}", "Направление": category}
    key = votes.card_key(probe)
    return any(votes.card_key(c) == key for c in visible(cards))


def create_card(
    ws: gspread.Worksheet,
    cards: list[dict],
    user: dict,
    category: str,
    name: str,
    text: str,
    normalize: Callable[[str], str],
) -> dict:
    """A new card written by the author themselves → verified at once. Needs a
    @username (the card is bound to the nick)."""
    username = user.get("username")
    if not _nick(username):
        raise CardUpdateError("Для объявления нужен username (@ник) в Telegram — укажи его в настройках.")
    category = normalize(_clean("Направление", category))
    name = _clean("Имя", name)
    text = _clean(votes.AUTHOR_TEXT_COL, text)
    if duplicate_exists(cards, username, category):
        raise CardUpdateError("У тебя уже есть объявление в этой категории — открой его и измени.")

    today = _today()
    values = {
        "Ник": f"@{_nick(username)}", "Имя": name, "Фамилия": "", "Направление": category,
        "Оригинальный текст объявления": text, "Дополнительные контакты": "",
        "Дата публикации": today, "Отзывы +": 0, "Отзывы -": 0,
        "ID автора": user.get("id") or "", votes.CONFIRMED_COL: today,
    }
    ws.append_row([values.get(h, "") for h in ws.row_values(1)], value_input_option="RAW")
    return values
