"""Who uses the service — one row per Telegram user in the "Пользователи" tab.

The bot (aiogram) and the mini app (Vercel) both write here, so this file takes
the worksheet as an argument and imports nothing from the project: the same
source is copied into the mini app as api/_usage.py by miniapp/sync_votes.py.

What is stored: Telegram ID, @nick, name, language, first/last visit, where the
person came from, counters (searches, voice notes, mini-app opens) and a status
("активен" / "заблокировал бота"). Search TEXTS are never stored — only counts.

Writing to Google Sheets is slow (~1 s) and rate limited, so the bot only
collects events in memory (add) and a background loop flushes them in one
batch (flush). The mini app writes its single event at once (record_now).
Column ownership keeps the two writers from overwriting each other: the bot
never touches "Открытий приложения"/"Последнее открытие", the app adds only
to those, and every counter is applied as "value in the sheet + delta".

Timestamps are UTC, "YYYY-MM-DD HH:MM:SS".
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Optional

import gspread

USERS_TAB = "Пользователи"
HEADER = [
    "ID", "Ник", "Имя", "Язык", "Первый визит", "Последний визит", "Источник",
    "Поисков (бот)", "Голосовых", "Открытий приложения", "Последнее открытие", "Статус",
]
(C_ID, C_NICK, C_NAME, C_LANG, C_FIRST, C_LAST, C_SOURCE,
 C_SEARCHES, C_VOICES, C_OPENS, C_LAST_OPEN, C_STATUS) = range(len(HEADER))

STATUS_ACTIVE = "активен"
STATUS_BLOCKED = "заблокировал бота"
SOURCE_BOT = "бот"
SOURCE_APP = "приложение"

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _stamp(when: Optional[dt.datetime] = None) -> str:
    return (when or _now()).strftime(TS_FORMAT)


def _parse(value: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(str(value).strip(), TS_FORMAT)
    except ValueError:
        return None


def _int(value: object) -> int:
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0


def _letter(index: int) -> str:
    return chr(ord("A") + index)        # the table has fewer than 26 columns


def _profile(user: dict) -> dict:
    username = str(user.get("username") or "").strip().lstrip("@")
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return {
        "nick": f"@{username}" if username else "",
        "name": name,
        "lang": str(user.get("language_code") or ""),
    }


def _merge(into: dict, src: dict) -> None:
    """Folds one pending event record into another (used on add and on a failed flush)."""
    for key in ("searches", "voices", "opens"):
        into[key] += src[key]
    for key in ("nick", "name", "lang"):
        if src[key]:
            into[key] = src[key]
    if src["status"] is not None:
        into["status"] = src["status"]
    into["seen"] = max(into["seen"], src["seen"])
    into["opened"] = max(into.get("opened", ""), src.get("opened", ""))


def _event(user: dict, source: str, searches: int, voices: int, opens: int,
           status: Optional[str]) -> dict:
    seen = _stamp()
    return {**_profile(user), "source": source, "searches": searches, "voices": voices,
            "opens": opens, "status": status, "seen": seen, "opened": seen if opens else ""}


class UsageLog:
    def __init__(self, get_ws: Callable[[], gspread.Worksheet]):
        # a provider, not a worksheet: the first call may hit the network and
        # must happen in the flushing thread, never in the bot's event loop
        self._get_ws = get_ws
        self._users_ws: Optional[gspread.Worksheet] = None
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()

    # ---- collecting (memory only, never blocks) ---------------------------

    def add(self, user: dict, source: str, *, searches: int = 0, voices: int = 0,
            opens: int = 0, status: Optional[str] = None) -> None:
        uid = user.get("id")
        if not uid or user.get("is_bot"):
            return
        event = _event(user, source, searches, voices, opens, status)
        with self._lock:
            if uid in self._pending:
                _merge(self._pending[uid], event)
            else:
                self._pending[uid] = event

    # ---- writing ----------------------------------------------------------

    def flush(self) -> int:
        """Writes everything collected so far in one batch. On failure the events
        are put back, so nothing is lost; returns how many users were written."""
        with self._flush_lock:
            with self._lock:
                batch, self._pending = self._pending, {}
            if not batch:
                return 0
            try:
                self._apply(batch)
            except Exception:
                with self._lock:
                    for uid, event in batch.items():
                        if uid in self._pending:
                            _merge(event, self._pending[uid])
                        self._pending[uid] = event
                raise
            return len(batch)

    def record_now(self, user: dict, source: str, **counts) -> None:
        """For the serverless mini app: one event, written immediately."""
        self.add(user, source, **counts)
        self.flush()

    def _tab(self) -> gspread.Worksheet:
        if self._users_ws is None:
            ss = self._get_ws().spreadsheet
            try:
                ws = ss.worksheet(USERS_TAB)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(title=USERS_TAB, rows=1000, cols=len(HEADER))
                ws.update("A1", [HEADER])
            self._users_ws = ws
        return self._users_ws

    def rows(self) -> list[list[str]]:
        """Raw rows of the tab, header first (for /stats)."""
        return self._tab().get_all_values()

    def _apply(self, batch: dict[int, dict]) -> None:
        ws = self._tab()
        rows = ws.get_all_values()
        where: dict[str, tuple[int, list[str]]] = {}
        for number, row in enumerate(rows[1:], start=2):
            if row and row[C_ID].strip():
                where.setdefault(row[C_ID].strip(), (number, row))

        updates, new_rows = [], []
        for uid, event in batch.items():
            status = event["status"] or STATUS_ACTIVE        # any activity = they're back
            if str(uid) in where:
                n, row = where[str(uid)]
                row = row + [""] * (len(HEADER) - len(row))
                updates.append({
                    "range": f"{_letter(C_NICK)}{n}:{_letter(C_VOICES)}{n}",
                    "values": [[event["nick"] or row[C_NICK], event["name"] or row[C_NAME],
                                event["lang"] or row[C_LANG], row[C_FIRST],
                                max(row[C_LAST], event["seen"]), row[C_SOURCE] or event["source"],
                                _int(row[C_SEARCHES]) + event["searches"],
                                _int(row[C_VOICES]) + event["voices"]]],
                })
                if event["opens"]:
                    updates.append({
                        "range": f"{_letter(C_OPENS)}{n}:{_letter(C_LAST_OPEN)}{n}",
                        "values": [[_int(row[C_OPENS]) + event["opens"], event["opened"]]],
                    })
                updates.append({"range": f"{_letter(C_STATUS)}{n}", "values": [[status]]})
            else:
                new_rows.append([
                    uid, event["nick"], event["name"], event["lang"], event["seen"], event["seen"],
                    event["source"], event["searches"], event["voices"], event["opens"],
                    event["opened"], status,
                ])
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")


# ---- reading ---------------------------------------------------------------

def summarize(rows: list[list[str]], now: Optional[dt.datetime] = None) -> dict:
    """Totals for /stats from the raw rows of the tab (header first). A person who
    ended up on two rows (the bot and the app both appended at once) counts once."""
    now = now or _now()
    people: dict[str, dict] = {}
    for row in rows[1:]:
        row = row + [""] * (len(HEADER) - len(row))
        uid = row[C_ID].strip()
        if not uid:
            continue
        first, last = _parse(row[C_FIRST]), _parse(row[C_LAST])
        p = people.setdefault(uid, {"first": first, "last": last, "source": row[C_SOURCE],
                                    "status": row[C_STATUS], "searches": 0, "voices": 0, "opens": 0})
        if first and (not p["first"] or first < p["first"]):
            p["first"], p["source"] = first, row[C_SOURCE]
        if last and (not p["last"] or last >= p["last"]):
            p["last"], p["status"] = last, row[C_STATUS]
        p["searches"] += _int(row[C_SEARCHES])
        p["voices"] += _int(row[C_VOICES])
        p["opens"] += _int(row[C_OPENS])

    def since(field: str, days: int) -> int:
        edge = now - dt.timedelta(days=days)
        return sum(1 for p in people.values() if p[field] and p[field] >= edge)

    return {
        "total": len(people),
        "new_1d": since("first", 1), "new_7d": since("first", 7), "new_30d": since("first", 30),
        "active_7d": since("last", 7),
        "blocked": sum(1 for p in people.values() if p["status"] == STATUS_BLOCKED),
        "from_bot": sum(1 for p in people.values() if p["source"] == SOURCE_BOT),
        "from_app": sum(1 for p in people.values() if p["source"] == SOURCE_APP),
        "searches": sum(p["searches"] for p in people.values()),
        "voices": sum(p["voices"] for p in people.values()),
        "app_users": sum(1 for p in people.values() if p["opens"]),
        "app_opens": sum(p["opens"] for p in people.values()),
    }
