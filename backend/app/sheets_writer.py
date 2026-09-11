"""Appends collected cards to the shared Google Sheet (TZ §5/§6).

One sheet for all chats, no source/channel column (not needed for the
final bot lookup use case per TZ §5). Column order matches TZ exactly:
Ник | Имя | Фамилия | Направление | Оригинальный текст объявления |
Дополнительные контакты | Дата публикации
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
]

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
            category or "",
            original_text,
            extra_contacts or "",
            published_at or "",
        ],
        value_input_option="RAW",
    )
