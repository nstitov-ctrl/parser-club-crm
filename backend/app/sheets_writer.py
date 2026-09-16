"""Appends collected cards to the shared Google Sheet (TZ §5/§6).

One sheet for all chats, no source/channel column (not needed for the
final bot lookup use case per TZ §5). Column order matches TZ, plus two
feedback columns appended at the end (not in the original TZ, added for
the bot's per-result 👍/👎 buttons):
Ник | Имя | Фамилия | Направление | Оригинальный текст объявления |
Дополнительные контакты | Дата публикации | Отзывы + | Отзывы -
"""
from __future__ import annotations

import json
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from app.config import settings

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

HEADER = [
    "Ник",
    "Имя",
    "Фамилия",
    "Направление",
    "Оригинальный текст объявления",
    "Дополнительные контакты",
    "Дата публикации",
    "Отзывы +",
    "Отзывы -",
]

# Known "same category, different wording" variants seen in the sheet —
# collapsed to one canonical name so counts don't fragment. Match is
# case-insensitive on the stripped raw value. Add new pairs here as they
# show up; don't force-merge genuinely different services just because
# they share a broad topic (e.g. different marketing specializations stay
# separate — only literal wording variants of the same thing get merged).
CATEGORY_ALIASES = {
    "доставка рационов правильного питания": "доставка рационов питания",
    "доставка готовых рационов питания": "доставка рационов питания",
    "членство в бизнес-клубе": "бизнес-клуб",
    "клиническая психология": "психология",
    "звуковое исцеление, звуковая терапия": "звуковая терапия",
    "преподавание игры на ханге": "обучение игре на ханге",
    "открытие банковского счета": "открытие банковских счетов в Индонезии",
    "аренда автомобилей и мотоциклов": "аренда автомобилей",
    "аренда мотоциклов": "аренда автомобилей",
    # "клубы" cluster — 15 different wordings for one service (get into a
    # club / book a table / guest list), canonicalized to the most common
    # existing label so counts stop fragmenting.
    "помощь с проходками в клубы": "проходки в клубы",
    "организация входа в клубы": "проходки в клубы",
    "организация вход в клубы и бронирование столов в ресторанах": "проходки в клубы",
    "организация гест-листов в ночные клубы": "проходки в клубы",
    "гест листы в клубы": "проходки в клубы",
    "гест листы в ночные клубы": "проходки в клубы",
    "гест листы и бронирование столиков в клубах": "проходки в клубы",
    "гест листы и бронирование столов в клубах и ресторанах": "проходки в клубы",
    "бронирование входов и столов в клубы": "проходки в клубы",
    "бронирование гест-листов и столов в клубах и ресторанах": "проходки в клубы",
    "бронирование гест-листов и столов в ночных клубах и ресторанах": "проходки в клубы",
    "бронирование столов и гест листы в клубы и рестораны": "проходки в клубы",
    "бронирование столов и гест-листы в барах и ресторанах": "проходки в клубы",
    "бронирование столов и гест-листы в клубах и ресторанах": "проходки в клубы",
    "промоутер клубов, организация входа и брони столиков": "проходки в клубы",
    # Separate from the above — this is club-side marketing/promotion, not
    # a customer-facing entry/table service, kept as its own category.
    "промоушн клубных мероприятий": "промоушн клубов",
    "промоушн ночных клубов": "промоушн клубов",
}


def normalize_category(category: Optional[str]) -> str:
    """Collapses known duplicate-wording categories to one canonical name
    (see CATEGORY_ALIASES) so both the scraper and the bot's manual-add
    flow keep the category list from fragmenting over time."""
    if not category:
        return ""
    stripped = category.strip()
    return CATEGORY_ALIASES.get(stripped.lower(), stripped)


_worksheet: Optional[gspread.Worksheet] = None


def _get_worksheet() -> gspread.Worksheet:
    global _worksheet
    if _worksheet is not None:
        return _worksheet

    if settings.google_service_account_json:
        # Hosted env (e.g. Railway) with no persistent filesystem — key
        # content comes from the env var directly, no file involved.
        info = json.loads(settings.google_service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            settings.google_service_account_file, scopes=_SCOPES
        )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    try:
        worksheet = spreadsheet.worksheet(settings.google_worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=settings.google_worksheet_name, rows=1000, cols=len(HEADER)
        )

    first_row = worksheet.row_values(1)
    if first_row != HEADER:
        worksheet.update("A1", [HEADER])

    _worksheet = worksheet
    return worksheet


def list_cards() -> list[dict]:
    """All cards currently in the sheet, as header->value dicts (for the bot's
    category search). Cheap enough for the expected table size; if it grows
    large this should move to a real cached store instead of a live read."""
    worksheet = _get_worksheet()
    return worksheet.get_all_records()


def list_cards_with_rows() -> list[dict]:
    """Same as list_cards(), but each dict also carries "_row" — its actual
    1-based row number in the sheet. Needed wherever a result has to be
    addressed later (e.g. the bot's feedback buttons write back to the
    exact row the user reacted to)."""
    worksheet = _get_worksheet()
    records = worksheet.get_all_records()
    for i, record in enumerate(records, start=2):  # row 1 is the header
        record["_row"] = i
    return records


def append_card(
    nickname: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    category: Optional[str],
    original_text: str,
    extra_contacts: Optional[str],
    published_at: Optional[str],
) -> None:
    worksheet = _get_worksheet()
    worksheet.append_row(
        [
            nickname or "",
            first_name or "",
            last_name or "",
            normalize_category(category),
            original_text,
            extra_contacts or "",
            published_at or "",
            0,
            0,
        ],
        value_input_option="RAW",
    )


def set_category(row: int, category: str) -> None:
    """Overwrites the Направление cell for one sheet row. One HTTP request
    per call — fine for a single ad-hoc rename, but the Sheets API write
    quota (per-minute, per-user) is easy to blow through renaming dozens
    of rows this way. Bulk renames (category_cleanup.py) should use
    set_categories_batch() instead."""
    worksheet = _get_worksheet()
    col_idx = HEADER.index("Направление") + 1
    worksheet.update_cell(row, col_idx, category)


def set_categories_batch(updates: dict[int, str]) -> None:
    """Renames the Направление cell for many rows in ONE Sheets API call
    (gspread batches Cell objects into a single values_batch_update
    request) — set_category() one row at a time hits the per-minute write
    quota once you're renaming dozens of rows (seen in practice cleaning
    up ~600 cards)."""
    if not updates:
        return
    worksheet = _get_worksheet()
    col_idx = HEADER.index("Направление") + 1
    cells = [gspread.Cell(row=row, col=col_idx, value=value) for row, value in updates.items()]
    worksheet.update_cells(cells, value_input_option="RAW")


def add_feedback(row: int, positive: bool) -> None:
    """Increments "Отзывы +" or "Отзывы -" for the card at the given sheet
    row (read-then-write on that single cell — feedback volume is low
    enough that the race window doesn't matter in practice)."""
    worksheet = _get_worksheet()
    column_name = "Отзывы +" if positive else "Отзывы -"
    col_idx = HEADER.index(column_name) + 1
    current_raw = worksheet.cell(row, col_idx).value
    try:
        current = int(current_raw) if current_raw else 0
    except ValueError:
        current = 0
    worksheet.update_cell(row, col_idx, current + 1)
