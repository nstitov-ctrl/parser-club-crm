"""Appends collected cards to the shared Google Sheet (TZ §5/§6).

One sheet for all chats, no source/channel column (not needed for the
final bot lookup use case per TZ §5). Column order matches TZ, plus two
feedback columns appended at the end (not in the original TZ, added for
the bot's per-result 👍/👎 buttons):
Ник | Имя | Фамилия | Направление | Оригинальный текст объявления |
Дополнительные контакты | Дата публикации | Отзывы + | Отзывы -
"""
from __future__ import annotations

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
