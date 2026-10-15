"""Appends collected cards to the shared Google Sheet (TZ §5/§6).

One sheet for all chats, no source/channel column (not needed for the
final bot lookup use case per TZ §5). Column order matches TZ, plus two
feedback columns appended at the end (not in the original TZ, added for
the bot's per-result 👍/👎 buttons):
Ник | Имя | Фамилия | Направление | Оригинальный текст объявления |
Дополнительные контакты | Дата публикации | Отзывы + | Отзывы -
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from app import votes
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
    # Numeric Telegram ID of the card's author — lets the voting rules block
    # self-votes even if the @username changes (see app/votes.py).
    "ID автора",
    # Author verification (check mark): date the author confirmed/corrected the
    # card (valid ~6 months), their corrected text (shown instead of the scraped
    # one), and a manual-cleanup marker ("УДАЛИТЬ") — rows are deleted by hand.
    "Подтверждено",
    "Текст от автора",
    "Удалить?",
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
    "членство в бизнес-клубе": "Бизнес-клуб и нетворкинг",
    "клиническая психология": "психология",
    "звуковое исцеление, звуковая терапия": "звуковая терапия",
    "преподавание игры на ханге": "обучение игре на ханге",
    "открытие банковского счета": "открытие банковских счетов в Индонезии",
    "аренда автомобилей и мотоциклов": "Аренда байков и автомобилей",
    "аренда мотоциклов": "Аренда байков и автомобилей",
    "аренда автомобилей": "Аренда байков и автомобилей",
    "аренда скутеров и мотоциклов": "Аренда байков и автомобилей",
    # Oct 2026 hand-curated pass over the 149 live categories (see
    # data/sheet_backup_*.json for the pre-merge snapshot) — only literal
    # wording variants of the same service, no broad-topic merging.
    "обучение языкам": "Обучение иностранным языкам",
    "индивидуальное обучение языкам": "Обучение иностранным языкам",
    "обучение онлайн, иностранные языки": "Обучение иностранным языкам",
    "обучение языку / переводы": "Обучение языкам и переводы",
    "обучение языку, переводы": "Обучение языкам и переводы",
    "преподавание китайского языка онлайн": "Обучение китайскому языку",
    "аренда вилл": "Аренда вилл и апартаментов",
    "аренда апартаментов": "Аренда вилл и апартаментов",
    "аренда барбершопа": "Аренда коммерческого помещения",
    "услуги личного шеф-повара": "Услуги частного повара",
    "букеты с ягодами в шоколаде": "Сладости и шоколадные подарки",
    "продажа клубники в шоколаде": "Сладости и шоколадные подарки",
    "продажа сладостей и шоколадных конфет": "Сладости и шоколадные подарки",
    "продажа шоколадных изделий": "Сладости и шоколадные подарки",
    "продажа рыбы": "Продажа рыбы и мяса",
    "продажа конины и мясных изделий": "Продажа рыбы и мяса",
    "уборка дома": "Клининг вилл и апартаментов",
    "домработница, уборка": "Клининг вилл и апартаментов",
    "подростковая психология": "Услуги психолога",
    "косметическое масло": "Косметика и пептиды",
    "поставка пептидов и косметики": "Косметика и пептиды",
    "ai ассистент с памятью": "Разработка веб и AI приложений",
    "управление проектами, разработка mvp и ai-инструментов": "Разработка веб и AI приложений",
    "реклама на такси": "Рекламные услуги",
    "бизнес-клуб": "Бизнес-клуб и нетворкинг",
    "профессиональный нетворкинг": "Бизнес-клуб и нетворкинг",
    "организация событий": "Организация мероприятий",
    "платформа для публикации мероприятий": "Организация мероприятий",
    "дисконтная программа, программа лояльности": "Программы лояльности",
    "программа лояльности и партнерские сервисы": "Программы лояльности",
    "интервью и контент-партнёрство": "Создание подкастов и медиа",
    "консультация при покупке бизнеса": "Консалтинг и аналитика",
    "продажа одежды и купальников": "Розница одежды и аксессуаров",
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
    stripped = " ".join(category.split())  # also collapses stray double spaces
    canonical = CATEGORY_ALIASES.get(stripped.lower(), stripped)
    # Consistent look in the bot / mini app: sentence case, first letter up
    # (the sheet had a mix of "проходки в клубы" and "Аренда байков…").
    return canonical[:1].upper() + canonical[1:]


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
        if worksheet.col_count < len(HEADER):      # new columns need grid room first
            worksheet.add_cols(len(HEADER) - worksheet.col_count)
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
    author_id: Optional[int] = None,
    confirmed_on: Optional[str] = None,
) -> None:
    """confirmed_on (ISO date) marks the card verified right away — used by the
    bot's "Добавить" flow, where the author wrote the card themselves."""
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
            author_id or "",
            confirmed_on or "",
            "",
            "",
        ],
        value_input_option="RAW",
    )


# ---- "Мои карточки": author verification by @nick ----------------------

def _norm_nick(value: object) -> str:
    return str(value or "").strip().lstrip("@").lower()


def find_my_cards(username: Optional[str]) -> list[dict]:
    """Cards whose "Ник" equals the caller's Telegram @username (case-insensitive).
    Verification is by nick only: whoever writes to the bot from that @username
    is treated as the card's author."""
    me = _norm_nick(username)
    if not me:
        return []
    return [c for c in list_cards_with_rows() if _norm_nick(c.get("Ник")) == me]


class CardUpdateError(Exception):
    """Refusal with a message that is safe to show to the user."""


def update_my_card(card_id: str, username: Optional[str], changes: dict[str, str]) -> dict:
    """Confirms (empty `changes`) or corrects one of the caller's cards and stamps
    today's date in "Подтверждено". Allowed keys: Имя, Направление, Текст от автора.
    Re-reads the sheet, so a stale row number can never hit the wrong card."""
    allowed = {"Имя", "Направление", votes.AUTHOR_TEXT_COL}
    if set(changes) - allowed:
        raise CardUpdateError("Это поле менять нельзя.")
    cards = list_cards_with_rows()
    card = next((c for c in cards if votes.card_id(c) == card_id), None)
    if card is None:
        raise CardUpdateError("Карточка изменилась — открой «Мои карточки» заново.")
    if not _norm_nick(username) or _norm_nick(card.get("Ник")) != _norm_nick(username):
        raise CardUpdateError("Эта карточка привязана к другому нику.")

    old_key = votes.card_key(card)
    updated = dict(card, **changes)
    new_key = votes.card_key(updated)
    if new_key != old_key and any(votes.card_key(c) == new_key for c in cards if c["_row"] != card["_row"]):
        raise CardUpdateError("У тебя уже есть карточка в этой категории.")

    today = dt.date.today().isoformat()
    ws = _get_worksheet()
    header = ws.row_values(1)
    cells = [gspread.Cell(card["_row"], header.index(k) + 1, v) for k, v in changes.items()]
    cells.append(gspread.Cell(card["_row"], header.index(votes.CONFIRMED_COL) + 1, today))
    ws.update_cells(cells, value_input_option="RAW")
    if new_key != old_key:
        get_ledger().migrate_key(old_key, new_key)      # votes/contacts keep following the card
    updated[votes.CONFIRMED_COL] = today
    return updated


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


_ledger: Optional[votes.Ledger] = None


def get_ledger() -> votes.Ledger:
    """Vote/contact ledger for this sheet. All votes go through it (app/votes.py) —
    there is deliberately no bare "+1" writer any more: it had no idea WHO was
    voting, so authors could vote for themselves and anyone could stack votes."""
    global _ledger
    if _ledger is None:
        _ledger = votes.Ledger(_get_worksheet())
    return _ledger
