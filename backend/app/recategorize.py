"""One-off remediation: re-derive every card's category from its original
ad text, ignoring whatever is currently in the sheet's Направление column.

Why this exists: category_cleanup.cleanup() run repeatedly back-to-back
against the full ~600-card backlog (rather than once per 50-card run, its
intended use) caused progressive over-merging — each pass only sees the
current category LABELS, not the original ad text, so by pass 3-4 the
model started collapsing genuinely different services (bathhouse, hair
salon, cosmetology-injections) into vague umbrella buckets ("банные
услуги, спа-центр"). The original_text column was never touched, so the
fix is to re-derive category strictly from ground truth text, batched to
keep the Haiku call count low (one call per ~20 ads, not one per ad).

Run once: `python -m app.recategorize`. Not part of the normal pipeline —
category_cleanup.cleanup() (single pass per scraper run) is what keeps
the sheet tidy day to day.
"""
from __future__ import annotations

import logging
from typing import Optional

import anthropic

from app import sheets_writer
from app.config import settings

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20

_SYSTEM_PROMPT = (
    "Тебе присылают несколько объявлений услуг с Бали (id + текст). Для "
    "каждого определи конкретную категорию услуги на русском языке — "
    "БУДЬ КОНКРЕТНЫМ, не обобщай. «Баня», «окрашивание волос», "
    "«косметология и инъекции» — это РАЗНЫЕ категории, даже если все "
    "про красоту/здоровье. «Аренда машин», «аренда вилл», «аренда "
    "байков» — РАЗНЫЕ категории, не «аренда» вообще. Не смешивай "
    "недвижимость и визы — это разные услуги. Категория должна описывать "
    "именно то, что предлагает конкретно ЭТО объявление, 2-5 слов. Если "
    "текст явно не объявление услуги (вакансия, вопрос, репост, разовое "
    "мероприятие с датой) — категория \"\" (пустая строка)."
)

_TOOL_SCHEMA = {
    "name": "assign_categories",
    "description": "Assigns a specific, non-generic Russian category to each ad.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "The id from the input, verbatim."},
                        "category": {
                            "type": "string",
                            "description": (
                                "Specific category in Russian (2-5 words), or "
                                "empty string if this isn't really a service ad."
                            ),
                        },
                    },
                    "required": ["id", "category"],
                },
            }
        },
        "required": ["items"],
    },
}

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _classify_batch(items: list[tuple[int, str]]) -> dict[int, str]:
    """items: list of (id, text). Returns {id: category} — ids the model
    doesn't return keep their current category (caller's responsibility,
    this function just reports what it got back)."""
    listing = "\n\n".join(f"id={i}:\n{text[:600]}" for i, text in items)
    try:
        response = _get_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=2000,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "assign_categories"},
            messages=[{"role": "user", "content": listing}],
        )
    except anthropic.APIError:
        logger.exception("recategorize: batch call failed, skipping this batch")
        return {}

    result: dict[int, str] = {}
    for block in response.content:
        if block.type != "tool_use" or block.name != "assign_categories":
            continue
        for entry in block.input.get("items", []):
            try:
                item_id = int(entry["id"])
            except (KeyError, TypeError, ValueError):
                continue
            result[item_id] = str(entry.get("category", "")).strip()
    return result


def run() -> int:
    """Re-derives category for every card from its original ad text.
    Returns how many cells changed."""
    cards = sheets_writer.list_cards_with_rows()
    logger.info("recategorize: %d cards to process", len(cards))

    id_to_row: dict[int, int] = {}
    id_to_old: dict[int, str] = {}
    batch: list[tuple[int, str]] = []
    all_results: dict[int, str] = {}

    for idx, card in enumerate(cards):
        text = str(card.get("Оригинальный текст объявления", "")).strip()
        if not text:
            continue
        id_to_row[idx] = card["_row"]
        id_to_old[idx] = str(card.get("Направление", "")).strip()
        batch.append((idx, text))
        if len(batch) >= _BATCH_SIZE:
            all_results.update(_classify_batch(batch))
            batch = []
    if batch:
        all_results.update(_classify_batch(batch))

    updates: dict[int, str] = {}
    for idx, new_category in all_results.items():
        new_category = sheets_writer.normalize_category(new_category)
        old_category = id_to_old.get(idx, "")
        if new_category and new_category != old_category:
            updates[id_to_row[idx]] = new_category

    logger.info("recategorize: %d cells to rewrite", len(updates))
    if updates:
        sheets_writer.set_categories_batch(updates)
    return len(updates)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    changed = run()
    print(f"пересчитано категорий: {changed}")
