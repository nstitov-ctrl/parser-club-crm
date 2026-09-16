"""SQLite storage: chats (with parsing position/status), collected cards (for
dedup before writing to Google Sheets) and run history (for progress UI).

Each function opens a short-lived connection — traffic is low (single local
user, manual "Start" button), so this keeps things simple and thread-safe
enough without a connection pool.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT UNIQUE NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'not_started',
    last_message_id INTEGER,
    last_message_date TEXT,
    messages_viewed INTEGER NOT NULL DEFAULT 0,
    cards_collected INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    message_id INTEGER,
    nickname TEXT,
    first_name TEXT,
    last_name TEXT,
    category TEXT,
    original_text TEXT NOT NULL,
    extra_contacts TEXT,
    published_at TEXT,
    dedup_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cards_collected INTEGER NOT NULL DEFAULT 0,
    messages_viewed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---- chats -----------------------------------------------------------------

def add_chat(identifier: str) -> sqlite3.Row:
    identifier = identifier.strip()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chats (identifier, status, added_at, updated_at) "
            "VALUES (?, 'not_started', ?, ?)",
            (identifier, _now(), _now()),
        )
        return conn.execute(
            "SELECT * FROM chats WHERE identifier = ?", (identifier,)
        ).fetchone()


def remove_chat(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM cards WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def list_chats() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM chats ORDER BY id").fetchall()


def get_next_active_chat() -> Optional[sqlite3.Row]:
    """First chat that is not fully done: resumes an in-progress chat first,
    otherwise picks the earliest-added not_started chat."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chats WHERE status = 'in_progress' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            return row
        return conn.execute(
            "SELECT * FROM chats WHERE status = 'not_started' ORDER BY id LIMIT 1"
        ).fetchone()


def mark_chat_in_progress(chat_id: int, title: Optional[str] = None) -> None:
    with get_conn() as conn:
        if title:
            conn.execute(
                "UPDATE chats SET status = 'in_progress', title = ?, updated_at = ? "
                "WHERE id = ? AND status = 'not_started'",
                (title, _now(), chat_id),
            )
        else:
            conn.execute(
                "UPDATE chats SET status = 'in_progress', updated_at = ? "
                "WHERE id = ? AND status = 'not_started'",
                (_now(), chat_id),
            )


def update_chat_position(
    chat_id: int,
    last_message_id: int,
    last_message_date: str,
    messages_viewed_delta: int = 1,
    cards_collected_delta: int = 0,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE chats SET last_message_id = ?, last_message_date = ?, "
            "messages_viewed = messages_viewed + ?, "
            "cards_collected = cards_collected + ?, updated_at = ? WHERE id = ?",
            (
                last_message_id,
                last_message_date,
                messages_viewed_delta,
                cards_collected_delta,
                _now(),
                chat_id,
            ),
        )


def mark_chat_done(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE chats SET status = 'done', updated_at = ? WHERE id = ?",
            (_now(), chat_id),
        )


# ---- cards -------------------------------------------------------------

def author_category_exists(nickname: Optional[str], category: Optional[str]) -> bool:
    """True if we already have a card from this exact author in this exact
    category — catches the case dedup_hash misses: the same person
    reposting the same offer reworded (different text, same author +
    category). Skipped (returns False) when either side is empty, so cards
    with no nickname/category never collide with each other on that basis
    alone."""
    nickname = (nickname or "").strip().lower().lstrip("@")
    category = (category or "").strip().lower()
    if not nickname or not category:
        return False
    with get_conn() as conn:
        # LTRIM(..., '@') so old rows saved without the leading @ (pre-fix)
        # still match against a now-@-prefixed nickname, and vice versa.
        row = conn.execute(
            "SELECT 1 FROM cards WHERE LTRIM(LOWER(TRIM(nickname)), '@') = ? "
            "AND LOWER(TRIM(category)) = ? LIMIT 1",
            (nickname, category),
        ).fetchone()
        return row is not None


def insert_card(
    chat_id: int,
    message_id: int,
    nickname: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    category: Optional[str],
    original_text: str,
    extra_contacts: Optional[str],
    published_at: Optional[str],
    dedup_hash: str,
) -> bool:
    """Returns False if this dedup_hash already exists (duplicate ad)."""
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO cards (chat_id, message_id, nickname, first_name, "
                "last_name, category, original_text, extra_contacts, "
                "published_at, dedup_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    message_id,
                    nickname,
                    first_name,
                    last_name,
                    category,
                    original_text,
                    extra_contacts,
                    published_at,
                    dedup_hash,
                    _now(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


# ---- runs ----------------------------------------------------------------

def create_run() -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            (_now(),),
        )
        return cur.lastrowid


def update_run_progress(run_id: int, cards_collected: int, messages_viewed: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE runs SET cards_collected = ?, messages_viewed = ? WHERE id = ?",
            (cards_collected, messages_viewed, run_id),
        )


def finish_run(run_id: int, status: str, error: Optional[str] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
            (_now(), status, error, run_id),
        )


def get_latest_run() -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()


def get_stats() -> dict:
    with get_conn() as conn:
        chats_total = conn.execute("SELECT COUNT(*) c FROM chats").fetchone()["c"]
        chats_done = conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE status = 'done'"
        ).fetchone()["c"]
        messages_viewed_total = conn.execute(
            "SELECT COALESCE(SUM(messages_viewed), 0) s FROM chats"
        ).fetchone()["s"]
        cards_total = conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"]
        return {
            "chats_total": chats_total,
            "chats_done": chats_done,
            "messages_viewed_total": messages_viewed_total,
            "cards_total": cards_total,
        }
