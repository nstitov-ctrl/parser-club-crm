"""Voting rules shared by the Telegram bot and the mini app.

Why this exists: a vote used to be a bare "+1" in the sheet — no record of
WHO voted, so an author could vote for their own card and anyone could stack
votes. The rules now live in one place so the two surfaces can't drift:

  * every vote and every "contact" click goes into an append-only ledger tab
    of the same Google Sheet ("Голоса" / "Контакты");
  * a user may vote on a card only AFTER pressing "Написать"/copying contact;
  * one standing vote per (user, card) — it can be changed (👍 <-> 👎) but not
    stacked;
  * at most MAX_VOTES_PER_DAY vote actions per account per rolling 24h;
  * authors can't vote on their own card (numeric Telegram ID when the card
    has one, otherwise by @username found in "Ник" / "Дополнительные контакты").

The "Отзывы +/-" cells on the card stay the fast read path (legacy votes made
before the ledger existed are preserved: counters are adjusted by deltas, not
recomputed).

Cards are identified by a stable key (author + category), NOT by sheet row —
rows shift whenever someone deletes rows. The copy of this file in
miniapp/api/_votes.py is generated from here (see miniapp/sync_votes.py).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread

MAX_VOTES_PER_DAY = 3
LEDGER_TAB = "Голоса"
CONTACTS_TAB = "Контакты"
LEDGER_HEADER = ["Время", "ID голосующего", "Ник голосующего", "Ключ карточки", "Голос"]
CONTACTS_HEADER = ["Время", "ID пользователя", "Ник пользователя", "Ключ карточки"]
AUTHOR_ID_COL = "ID автора"
POS_COL, NEG_COL = "Отзывы +", "Отзывы -"


class VoteError(Exception):
    """A rule refused the action; `message` is safe to show to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- card identity ------------------------------------------------------

def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def author_key(card: dict) -> str:
    nick = _norm(card.get("Ник")).lstrip("@")
    return nick or _norm(card.get("Дополнительные контакты"))


def card_key(card: dict) -> str:
    """One author + one category = one card (the project's dedup rule)."""
    return f"{author_key(card)}|{_norm(card.get('Направление'))}"


def card_id(card: dict) -> str:
    """Short opaque id for callback_data / URLs (Telegram caps callback_data at 64 bytes)."""
    return hashlib.sha1(card_key(card).encode("utf-8")).hexdigest()[:12]


def as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def net_score(card: dict) -> int:
    return as_int(card.get(POS_COL)) - as_int(card.get(NEG_COL))


def sort_by_rating(cards: list[dict], rank: Optional[dict[str, int]] = None) -> list[dict]:
    """Best-rated first: biggest (👍 − 👎), then more 👍, then search relevance
    (position of the card's category in `rank`), then original order."""
    def key(item: tuple[int, dict]):
        i, c = item
        rel = rank.get(str(c.get("Направление", "")).strip(), 0) if rank else 0
        return (-net_score(c), -as_int(c.get(POS_COL)), rel, i)
    return [c for _, c in sorted(enumerate(cards), key=key)]


# ---- self-vote check ----------------------------------------------------

_HANDLE_RE = re.compile(r"(?:@|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/)([A-Za-z][A-Za-z0-9_]{3,31})", re.I)


def author_handles(card: dict) -> set[str]:
    handles = set()
    nick = _norm(card.get("Ник")).lstrip("@")
    if nick:
        handles.add(nick)
    for m in _HANDLE_RE.finditer(str(card.get("Дополнительные контакты") or "")):
        handles.add(m.group(1).lower())
    return handles


def is_own_card(card: dict, user: dict) -> bool:
    """Numeric Telegram ID wins (survives username changes); @username is the
    fallback for cards that don't have an ID yet."""
    author_id = str(card.get(AUTHOR_ID_COL) or "").strip()
    if author_id and author_id == str(user.get("id", "")).strip():
        return True
    username = _norm(user.get("username")).lstrip("@")
    return bool(username and username in author_handles(card))


# ---- ledger (append-only, in the same spreadsheet) ----------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime:
    try:
        ts = datetime.fromisoformat(str(value))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


class Ledger:
    """Reads/writes the "Голоса" and "Контакты" tabs and applies the rules."""

    def __init__(self, cards_ws: gspread.Worksheet):
        self.cards_ws = cards_ws
        self._tabs: dict[str, gspread.Worksheet] = {}

    # tabs
    def _tab(self, name: str, header: list[str]) -> gspread.Worksheet:
        if name not in self._tabs:
            ss = self.cards_ws.spreadsheet
            try:
                ws = ss.worksheet(name)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(title=name, rows=1000, cols=len(header))
                ws.update("A1", [header])
            self._tabs[name] = ws
        return self._tabs[name]

    @property
    def _votes_ws(self) -> gspread.Worksheet:
        return self._tab(LEDGER_TAB, LEDGER_HEADER)

    @property
    def _contacts_ws(self) -> gspread.Worksheet:
        return self._tab(CONTACTS_TAB, CONTACTS_HEADER)

    # reads
    def _vote_rows(self, user_id: object) -> list[dict]:
        uid = str(user_id)
        return [r for r in self._votes_ws.get_all_records() if str(r.get("ID голосующего", "")) == uid]

    def contacted_keys(self, user_id: object) -> set[str]:
        uid = str(user_id)
        return {str(r.get("Ключ карточки", "")) for r in self._contacts_ws.get_all_records()
                if str(r.get("ID пользователя", "")) == uid}

    def user_state(self, user_id: object) -> dict:
        """Everything the UI needs about this user: standing votes, contacted
        cards and how many vote actions are left today."""
        rows = self._vote_rows(user_id)
        standing: dict[str, int] = {}
        for r in rows:                                   # ledger is chronological: last row wins
            standing[str(r.get("Ключ карточки", ""))] = as_int(r.get("Голос"))
        cutoff = _now() - timedelta(hours=24)
        recent = [r for r in rows if _parse_ts(r.get("Время")) >= cutoff]
        return {
            "votes": standing,
            "contacted": self.contacted_keys(user_id),
            "left": max(0, MAX_VOTES_PER_DAY - len(recent)),
            "recent": recent,
        }

    # writes
    def record_contact(self, card: dict, user: dict) -> bool:
        """Remember that this user reached out to this card. Returns True if new."""
        key = card_key(card)
        if key in self.contacted_keys(user.get("id")):
            return False
        self._contacts_ws.append_row(
            [_now().isoformat(), str(user.get("id", "")), str(user.get("username") or ""), key],
            value_input_option="RAW",
        )
        return True

    def cast_vote(self, cards: list[dict], cid: str, user: dict, positive: bool) -> dict:
        """Applies every rule, then writes the ledger row and adjusts the
        card's counters. `cards` must be a fresh read with "_row" on each."""
        card = next((c for c in cards if card_id(c) == cid), None)
        if card is None:
            raise VoteError("gone", "Карточка изменилась — обновите список.")
        if is_own_card(card, user):
            raise VoteError("own", "Свою карточку оценивать нельзя.")

        state = self.user_state(user.get("id"))
        key = card_key(card)
        if key not in state["contacted"]:
            raise VoteError("no_contact", "Оценить можно после контакта: сначала нажмите «Написать».")

        new = 1 if positive else -1
        old = state["votes"].get(key, 0)
        if old == new:
            return {"changed": False, "vote": new, "pos": as_int(card.get(POS_COL)),
                    "neg": as_int(card.get(NEG_COL)), "left": state["left"]}
        if state["left"] <= 0:
            oldest = min(_parse_ts(r.get("Время")) for r in state["recent"])
            hours = max(1, int((oldest + timedelta(hours=24) - _now()).total_seconds() // 3600) + 1)
            raise VoteError("limit", f"Лимит — {MAX_VOTES_PER_DAY} оценки в сутки. Следующая будет доступна примерно через {hours} ч.")

        self._votes_ws.append_row(
            [_now().isoformat(), str(user.get("id", "")), str(user.get("username") or ""), key, new],
            value_input_option="RAW",
        )
        pos, neg = self._adjust_counters(card, old, new)
        return {"changed": True, "vote": new, "pos": pos, "neg": neg, "left": state["left"] - 1}

    def _adjust_counters(self, card: dict, old: int, new: int) -> tuple[int, int]:
        ws = self.cards_ws
        header = ws.row_values(1)
        pos_col, neg_col = header.index(POS_COL) + 1, header.index(NEG_COL) + 1
        row = card["_row"]
        pos = as_int(ws.cell(row, pos_col).value)
        neg = as_int(ws.cell(row, neg_col).value)
        if old == 1:
            pos = max(0, pos - 1)
        elif old == -1:
            neg = max(0, neg - 1)
        if new == 1:
            pos += 1
        else:
            neg += 1
        ws.update_cells([gspread.Cell(row, pos_col, pos), gspread.Cell(row, neg_col, neg)],
                        value_input_option="RAW")
        return pos, neg
