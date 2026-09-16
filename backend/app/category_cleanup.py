"""Automatic category cleanup — runs once after each scraper pass finishes.

The deterministic alias table (sheets_writer.CATEGORY_ALIASES) catches
known wording variants for free at write time, but brand-new variants
(typos, fresh phrasings the classifier happens to produce) keep showing
up — this closes that gap with ONE cheap Haiku call per run: send it the
full distinct-category list currently in the sheet, it clusters obvious
near-duplicates and picks a canonical name per cluster, then only the
cells that actually changed get rewritten. Cost is tiny (short category
strings, not full ad text) and bounded to once per run, not once per
card — matches the project's cheapest-model discipline (see filtering.py).
"""
from __future__ import annotations

import logging
from typing import Optional

import anthropic

from app import sheets_writer
from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Тебе дают список категорий услуг из CRM. Некоторые строки — разные "
    "формулировки ОДНОЙ И ТОЙ ЖЕ услуги (опечатки, синонимы, разный "
    "порядок слов, с уточнениями и без). Сгруппируй ТОЛЬКО такие явные "
    "дубли по смыслу. НЕ объединяй разные специализации одной широкой "
    "темы — например разные виды маркетинга, разные языки в обучении, "
    "разные виды дизайна остаются отдельными категориями, если это не "
    "буквально одна и та же услуга другими словами. Для каждой найденной "
    "группы дублей верни один канонический вариант — обязательно один из "
    "уже присланных, не выдумывай новый текст — и полный список "
    "формулировок из этой группы (включая сам канонический вариант). "
    "Категории без дублей вообще не включай в ответ."
)

_TOOL_SCHEMA = {
    "name": "report_category_clusters",
    "description": (
        "Groups of category strings from the input list that mean the "
        "same service, each with a chosen canonical name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical": {
                            "type": "string",
                            "description": (
                                "The chosen name for this cluster — must be "
                                "one of the input categories, verbatim."
                            ),
                        },
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "All category strings from the input that "
                                "belong to this cluster, verbatim, "
                                "including the canonical one."
                            ),
                        },
                    },
                    "required": ["canonical", "members"],
                },
            }
        },
        "required": ["clusters"],
    },
}

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _suggest_merges(categories: list[str]) -> dict[str, str]:
    """Returns {original: canonical} only for categories that should be
    renamed — a cluster's own canonical entry is never included (renaming
    it to itself would be a no-op anyway)."""
    if len(categories) < 2:
        return {}

    listing = "\n".join(f"- {c}" for c in sorted(categories))
    try:
        response = _get_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=8000,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "report_category_clusters"},
            messages=[{"role": "user", "content": f"Категории:\n{listing}"}],
        )
    except anthropic.APIError:
        logger.exception("category cleanup call failed, skipping this pass")
        return {}

    category_set = set(categories)
    mapping: dict[str, str] = {}
    for block in response.content:
        if block.type != "tool_use" or block.name != "report_category_clusters":
            continue
        for cluster in block.input.get("clusters", []):
            canonical = cluster.get("canonical", "")
            if canonical not in category_set:
                continue  # model invented text outside the input — ignore
            for member in cluster.get("members", []):
                if member in category_set and member != canonical:
                    mapping[member] = canonical
    return mapping


def cleanup() -> int:
    """Normalizes category wording across the whole sheet: first the free,
    deterministic alias table, then one Haiku call to catch whatever that
    table doesn't know about yet. Returns how many cells were rewritten.
    Never raises — a failed cleanup pass shouldn't take down the run that
    triggered it, next run's pass will just catch it instead."""
    try:
        cards = sheets_writer.list_cards_with_rows()
    except Exception:
        logger.exception("category cleanup: failed to read the sheet, skipping")
        return 0

    updates: dict[int, str] = {}

    # Pass 1: free, deterministic aliases (known pairs, no API call).
    for card in cards:
        old = str(card.get("Направление", "")).strip()
        new = sheets_writer.normalize_category(old)
        if new and new != old:
            updates[card["_row"]] = new
            card["Направление"] = new

    # Pass 2: one cheap LLM call for whatever pass 1 missed (new wordings).
    categories = sorted(
        {str(c.get("Направление", "")).strip() for c in cards if str(c.get("Направление", "")).strip()}
    )
    mapping = _suggest_merges(categories)
    for card in cards:
        old = str(card.get("Направление", "")).strip()
        new = mapping.get(old)
        if new and new != old:
            updates[card["_row"]] = new

    # One batched write for everything — set_category() per cell hits the
    # Sheets API's per-minute write quota once there are dozens of renames
    # (hit this for real cleaning up ~600 cards).
    if updates:
        try:
            sheets_writer.set_categories_batch(updates)
        except Exception:
            logger.exception("category cleanup: batch write failed, nothing renamed this pass")
            return 0

    changed = len(updates)
    if changed:
        logger.info("category cleanup: renamed %d cells", changed)
    return changed
