"""CariOra Mini App API (Vercel, FastAPI).

Self-contained on purpose: backend/app creates a data/ directory at import
time, which a read-only serverless filesystem can't do. Reads the same
Google Sheet as the bot and uses the same semantic category match, so the
two surfaces always agree.

Every endpoint requires Telegram's signed initData (X-Telegram-Init-Data
header), verified against the bot token — the page only works when opened
from inside Telegram.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import parse_qsl

import anthropic
import gspread
from fastapi import FastAPI, Header, HTTPException
from google.oauth2.service_account import Credentials
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _match_prompt import SYSTEM_PROMPT, TOOL_SCHEMA  # noqa: E402
from _votes import (  # noqa: E402  (generated copy of backend/app/votes.py)
    MAX_VOTES_PER_DAY, Ledger, VoteError, card_id, card_key, display_text, is_deleted, is_own_card,
    is_verified, sort_by_rating,
)
from _usage import SOURCE_APP, UsageLog  # noqa: E402  (generated: the "Пользователи" tab shared with the bot)
from _aliases import normalize_category  # noqa: E402  (generated: same category aliases as the bot)
from _authoring import (  # noqa: E402  (generated: author actions shared with the bot)
    CardUpdateError, create_card, delete_card, find_my_cards, update_card,
)
from _forbidden import SYSTEM_PROMPT as FORBIDDEN_PROMPT, TOOL_SCHEMA as FORBIDDEN_SCHEMA  # noqa: E402

logger = logging.getLogger("miniapp")

app = FastAPI(title="CariOra Mini App API")

MAX_RESULTS = 30
CACHE_TTL = 20          # seconds the sheet is kept in memory per warm instance (a delete in the bot shows up fast)
INIT_DATA_MAX_AGE = 86400

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")

_worksheet: Optional[gspread.Worksheet] = None
_cache: dict = {"at": 0.0, "cards": []}
_ai: Optional[anthropic.Anthropic] = None


# ---- auth ---------------------------------------------------------------

def _verify_init_data(init_data: Optional[str]) -> dict:
    """Validates Telegram WebApp initData (HMAC-SHA256 keyed with the bot
    token) and returns the user dict. Raises 401 on anything off."""
    if not init_data:
        raise HTTPException(status_code=401, detail="Откройте приложение из Telegram")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", os.environ["TELEGRAM_BOT_TOKEN"].encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=401, detail="Не удалось подтвердить Telegram")
    try:
        age = time.time() - int(pairs.get("auth_date", "0"))
    except ValueError:
        age = INIT_DATA_MAX_AGE + 1
    if age > INIT_DATA_MAX_AGE:
        raise HTTPException(status_code=401, detail="Сессия устарела, откройте приложение заново")
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return {}


# ---- sheet --------------------------------------------------------------

def _get_worksheet() -> gspread.Worksheet:
    global _worksheet
    if _worksheet is None:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        spreadsheet = gspread.authorize(creds).open_by_key(os.environ["GOOGLE_SHEET_ID"])
        _worksheet = spreadsheet.worksheet(os.getenv("GOOGLE_WORKSHEET_NAME", "Карточки"))
    return _worksheet


def _load_cards(force: bool = False) -> list[dict]:
    if not force and _cache["cards"] and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["cards"]
    try:
        records = _get_worksheet().get_all_records()
    except Exception:
        logger.exception("sheet read failed")
        # Search may show slightly old data rather than an error screen, but a forced read
        # (cabinet, edit, delete, vote) must never fall back to a stale cache: it would show
        # — or act on — cards that were just deleted or changed.
        if not force and _cache["cards"]:
            return _cache["cards"]
        raise HTTPException(status_code=503, detail="Не получилось прочитать базу, попробуйте позже")
    for i, record in enumerate(records, start=2):  # row 1 is the header
        record["_row"] = i
    # cards an author deleted stay in the sheet but are hidden everywhere
    # ("_row" is assigned first, so the rest keep their true row)
    records = [r for r in records if not is_deleted(r)]
    _cache.update(at=time.time(), cards=records)
    return records


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _category(card: dict) -> str:
    return str(card.get("Направление", "")).strip()


def _contact_key(card: dict) -> str:
    nick = str(card.get("Ник", "")).strip().lower()
    return nick or str(card.get("Дополнительные контакты", "")).strip().lower()


def _format_contact(value: str) -> str:
    """Bare usernames get their @ back; links/phones pass through."""
    parts = [p.strip() for p in value.split(",")]
    return ", ".join(
        f"@{p}" if p and not p.startswith("@") and _USERNAME_RE.match(p) else p for p in parts
    )


def _tg_username(contact: str) -> Optional[str]:
    for part in contact.split(","):
        match = _USERNAME_RE.match(part.strip())
        if match:
            return match.group(1)
    return None


_ledger_obj: Optional[Ledger] = None


def _ledger() -> Ledger:
    global _ledger_obj
    if _ledger_obj is None:
        _ledger_obj = Ledger(_get_worksheet())
    return _ledger_obj


def _public(card: dict, state: dict, user: dict) -> dict:
    """Card as the page sees it. `state` is this user's ledger state, so the
    page knows what is already unlocked/voted without extra requests."""
    name = f"{card.get('Имя', '')} {card.get('Фамилия', '')}".strip() or str(card.get("Ник") or "Без имени")
    contact = _format_contact(str(card.get("Дополнительные контакты") or card.get("Ник") or ""))
    key = card_key(card)
    return {
        "id": card_id(card),
        "name": name,
        "category": _category(card),
        "text": display_text(card),                 # the author's corrected text wins over the scraped one
        "verified": is_verified(card),              # author confirmed the card within ~6 months → check mark
        "contact": contact,
        "username": _tg_username(contact),
        "pos": _as_int(card.get("Отзывы +")),
        "neg": _as_int(card.get("Отзывы -")),
        "own": is_own_card(card, user),             # author: can't rate own card
        "contacted": key in state["contacted"],     # unlocks voting (pressed "Написать"/copied)
        "my_vote": state["votes"].get(key, 0),      # +1 / -1 / 0
    }


# ---- search -------------------------------------------------------------

def _match_categories(query: str, categories: list[str]) -> Optional[list[str]]:
    """Same semantic match as the bot. None = AI unavailable (caller falls
    back to substring search)."""
    global _ai
    if not categories:
        return []
    try:
        if _ai is None:
            _ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        listing = "\n".join(f"- {c}" for c in categories)
        response = _ai.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=1000,
            temperature=0,
            system=SYSTEM_PROMPT,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "match_categories"},
            messages=[{"role": "user", "content": f"Категории:\n{listing}\n\nЗапрос пользователя: {query}"}],
        )
    except Exception:
        logger.exception("semantic match failed, falling back to substring search")
        return None
    known = set(categories)
    matched: list[str] = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "match_categories":
            for cat in block.input.get("matched_categories", []):
                if cat in known and cat not in matched:
                    matched.append(cat)
    return matched


def _dedupe(cards: list[dict]) -> list[dict]:
    """One card per contact — a repost shouldn't eat several result slots."""
    seen: set[str] = set()
    out = []
    for card in cards:
        key = _contact_key(card)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(card)
    return out


_usage_obj: Optional[UsageLog] = None


@app.post("/api/hello")
def hello(x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    """Called once per app session: counts the open in the "Пользователи" tab.
    Never fails the app — statistics are not worth an error screen."""
    global _usage_obj
    user = _verify_init_data(x_telegram_init_data)
    try:
        if _usage_obj is None:
            _usage_obj = UsageLog(_get_worksheet)
        _usage_obj.record_now(user, SOURCE_APP, opens=1)
    except Exception:
        logger.exception("usage logging failed")
    return {"ok": True}


@app.get("/api/categories")
def categories(x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    _verify_init_data(x_telegram_init_data)
    counts: dict[str, int] = {}
    for card in _load_cards():
        cat = _category(card)
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"categories": [{"name": n, "count": c} for n, c in ordered], "total": sum(counts.values())}


@app.get("/api/search")
def search(
    q: str = "",
    category: str = "",
    x_telegram_init_data: Optional[str] = Header(None),
) -> dict:
    user = _verify_init_data(x_telegram_init_data)
    q, category = q.strip(), category.strip()
    if not q and not category:
        raise HTTPException(status_code=400, detail="Введите запрос или выберите категорию")

    cards = [c for c in _load_cards() if _category(c)]
    mode = "category"
    rank = None
    if category:
        matches = [c for c in cards if _category(c) == category]
    else:
        cats = sorted({_category(c) for c in cards})
        matched = _match_categories(q, cats)
        if matched is not None:
            mode = "semantic"
            rank = {cat: i for i, cat in enumerate(matched)}
            matches = [c for c in cards if _category(c) in rank]
        else:
            mode = "substring"
            matches = [c for c in cards if q.lower() in _category(c).lower()]

    # Best-rated first (👍 − 👎, then 👍; relevance only breaks ties) — BEFORE the
    # per-contact dedupe so the best copy of a repost is the one that survives.
    matches = _dedupe(sort_by_rating(matches, rank))

    try:
        state = _ledger().user_state(user.get("id"))
    except Exception:
        logger.exception("ledger read failed")
        raise HTTPException(status_code=503, detail="Не получилось прочитать оценки, попробуйте позже")
    return {
        "mode": mode,
        "total": len(matches),
        "votes_left": state["left"],
        "votes_per_day": MAX_VOTES_PER_DAY,
        "items": [_public(c, state, user) for c in matches[:MAX_RESULTS]],
    }


# ---- feedback -----------------------------------------------------------

class ContactRequest(BaseModel):
    id: str


class FeedbackRequest(BaseModel):
    id: str
    positive: bool


_VOTE_STATUS = {"gone": 409, "own": 403, "no_contact": 403, "limit": 429}


@app.post("/api/contact")
def contact(payload: ContactRequest, x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    """"Написать" / "Копировать" pressed — records that this user reached out
    to this card, which is what unlocks rating it."""
    user = _verify_init_data(x_telegram_init_data)
    card = next((c for c in _load_cards(force=True) if card_id(c) == payload.id), None)
    if card is None:
        raise HTTPException(status_code=409, detail="Карточка изменилась — обновите список.")
    try:
        _ledger().record_contact(card, user)
    except Exception:
        logger.exception("contact record failed")
        raise HTTPException(status_code=503, detail="Не получилось сохранить, попробуйте ещё раз")
    return {"ok": True}


@app.post("/api/feedback")
def feedback(payload: FeedbackRequest, x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    """One 👍/👎. All rules (own card, contact first, one standing vote that can
    be changed, 3 actions/day) live in _votes.Ledger.cast_vote — shared with the bot."""
    user = _verify_init_data(x_telegram_init_data)
    try:
        result = _ledger().cast_vote(_load_cards(force=True), payload.id, user, payload.positive)
    except VoteError as err:
        raise HTTPException(status_code=_VOTE_STATUS.get(err.code, 400), detail=err.message)
    except HTTPException:
        raise
    except Exception:
        logger.exception("vote failed")
        raise HTTPException(status_code=503, detail="Не получилось сохранить оценку, попробуйте позже")
    _cache["at"] = 0.0  # next read picks the new counters up
    return {"ok": True, **result}


# ---- Личный кабинет -------------------------------------------------------
# An author is whoever opens the app from the @username in a card's "Ник".
# Every rule (own cards only, one card per category, soft delete, 6-month check
# mark) lives in _authoring.py — shared with the bot.

def _client() -> anthropic.Anthropic:
    global _ai
    if _ai is None:
        _ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _ai


def _forbidden_reason(category: str, name: str, text: str) -> Optional[str]:
    """Same forbidden-topic check as the bot's add/edit flows. Fails open: an AI
    hiccup shouldn't block a legitimate card."""
    content = f"Категория: {category}\nИмя/название: {name}\nОписание: {text}"
    try:
        response = _client().messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=200,
            temperature=0,
            system=FORBIDDEN_PROMPT,
            tools=[FORBIDDEN_SCHEMA],
            tool_choice={"type": "tool", "name": "check_forbidden_topic"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        logger.exception("forbidden-topic check failed, allowing (fail-open)")
        return None
    for block in response.content:
        if block.type == "tool_use" and block.name == "check_forbidden_topic" and block.input.get("is_forbidden"):
            return str(block.input.get("reason") or "запрещённая тема")
    return None


def _mine_public(card: dict) -> dict:
    return {
        "id": card_id(card),
        "name": str(card.get("Имя") or card.get("Ник") or ""),
        "category": _category(card),
        "text": display_text(card),
        "verified": is_verified(card),
    }


def _author_action(fn):
    """Runs an author action; turns the shared rules' refusals into HTTP errors."""
    try:
        return fn()
    except CardUpdateError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except HTTPException:
        raise
    except Exception:
        logger.exception("author action failed")
        raise HTTPException(status_code=503, detail="Не получилось сохранить, попробуйте позже")
    finally:
        _cache["at"] = 0.0  # search picks the change up at once


class CardIdRequest(BaseModel):
    id: str


class CardEditRequest(BaseModel):
    id: str
    name: Optional[str] = None
    category: Optional[str] = None
    text: Optional[str] = None


class CardCreateRequest(BaseModel):
    category: str
    name: str
    text: str


@app.get("/api/me/cards")
def my_cards(x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    user = _verify_init_data(x_telegram_init_data)
    username = user.get("username")
    cards = _author_action(lambda: find_my_cards(_load_cards(force=True), username))
    return {"username": username, "can_create": bool(username), "items": [_mine_public(c) for c in cards]}


@app.post("/api/me/confirm")
def my_confirm(payload: CardIdRequest, x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    user = _verify_init_data(x_telegram_init_data)
    card = _author_action(lambda: update_card(
        _get_worksheet(), _ledger(), _load_cards(force=True), payload.id, user.get("username"), {}))
    return {"ok": True, "item": _mine_public(card)}


@app.post("/api/me/update")
def my_update(payload: CardEditRequest, x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    user = _verify_init_data(x_telegram_init_data)
    username = user.get("username")

    def run() -> dict:
        cards = _load_cards(force=True)
        mine = next((c for c in find_my_cards(cards, username) if card_id(c) == payload.id), None)
        if mine is None:
            raise CardUpdateError("Карточка изменилась или удалена — обновите список.")
        changes = {}
        if payload.name is not None:
            changes["Имя"] = payload.name
        if payload.category is not None:
            changes["Направление"] = normalize_category(payload.category)
        if payload.text is not None:
            changes["Текст от автора"] = payload.text
        reason = _forbidden_reason(
            changes.get("Направление", _category(mine)),
            changes.get("Имя", str(mine.get("Имя") or mine.get("Ник") or "")),
            changes.get("Текст от автора", display_text(mine)),
        )
        if reason:
            raise HTTPException(status_code=422, detail=f"Не получилось сохранить: похоже на запрещённую тему ({reason}).")
        return update_card(_get_worksheet(), _ledger(), cards, payload.id, username, changes)

    return {"ok": True, "item": _mine_public(_author_action(run))}


@app.post("/api/me/delete")
def my_delete(payload: CardIdRequest, x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    user = _verify_init_data(x_telegram_init_data)
    _author_action(lambda: delete_card(_get_worksheet(), _load_cards(force=True), payload.id, user.get("username")))
    return {"ok": True}


@app.post("/api/me/create")
def my_create(payload: CardCreateRequest, x_telegram_init_data: Optional[str] = Header(None)) -> dict:
    user = _verify_init_data(x_telegram_init_data)

    def run() -> dict:
        category = normalize_category(payload.category)
        reason = _forbidden_reason(category, payload.name, payload.text)
        if reason:
            raise HTTPException(status_code=422, detail=f"Не получилось сохранить: похоже на запрещённую тему ({reason}).")
        return create_card(_get_worksheet(), _load_cards(force=True), user, category, payload.name,
                           payload.text, normalize_category)

    values = _author_action(run)
    return {"ok": True, "item": {"name": values["Имя"], "category": values["Направление"]}}
