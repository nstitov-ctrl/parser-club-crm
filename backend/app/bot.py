"""@CariOra_bot — Telegram Bot API bot (separate from the Telethon scraper
account). Flows:

- "Добавить" — user dictates an expert/product card (category, name,
  description — no contacts question, contact is always the Telegram
  handle of whoever is chatting) through a short FSM, saved into the same
  Google Sheet the scraper writes to (see sheets_writer.py) so both
  sources feed one shared table. Description capped at _MAX_TEXT_LEN
  (1000) chars. Before saving, a cheap Haiku call checks the category/
  name/description against a forbidden-topics list (drugs, escort
  services, weapons, fraud, etc.) and blocks the save if it matches —
  fails open (allows the save) if the check itself errors, so an API
  hiccup never blocks a legitimate submission. The confirm screen has an
  "✏️ Изменить" button — pick a field, retype it, land back on confirm
  with the updated summary (catches misheard voice input, e.g. a voiced
  "Бали" transcribed as "боли").
- "Найти" — user types free text describing what they want (not a category
  name — "нужна татуировка" finds "татуировка" even without literal word
  overlap). Matched BY MEANING via Claude Haiku against the live category
  list (_match_categories_semantic) — categories aren't from a fixed list,
  see filtering.py. Falls back to plain substring matching only if the AI
  call fails (e.g. no API credit), so search never goes fully dark. Reply
  starts with the real total match count (bold), then at most 3
  best-matching cards (fewer if fewer exist), ranked by category
  relevance as the AI returned it. Each shown card is one line: author name
  (falls back to
  their "Ник" — the poster's own handle — if no name was extracted), a
  genuine one-line LLM summary of what the ad offers (never a truncated
  slice of the original text), contact, and — if the card already has
  feedback — a short "N positive / N negative" note. Each shown card also
  gets a 👍/👎 button pair; a tap writes into the sheet's Отзывы +/-
  columns (see sheets_writer.py) and is what powers that note.
- Voice messages — transcribed locally (see voice.py, faster-whisper, no
  API cost) and treated exactly like typed text, in every flow above
  (FSM steps, find, free-form question).
- Anything else typed outside these flows — free-form question — goes to a
  small Claude Haiku agent (same cheapest-tier model as the scraper's
  filter, see filtering.py) grounded ONLY in the live category/count list,
  so it can answer things like "какие категории есть?" in its own words
  without inventing data. It does not search or write cards itself — for
  those it points back at the menu buttons, which stay the deterministic
  path.

No moderation queue: matches the scraper's existing behavior (cards go
straight into the sheet, no approval step) rather than inventing a new one.
Run standalone: `python bot_main.py` (separate process from the FastAPI CRM).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import re
from typing import Any, Awaitable, Callable, Optional

import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app import sheets_writer, voice
from app.config import settings

logger = logging.getLogger(__name__)

router = Router()


@router.message.middleware()
async def _voice_transcription_middleware(
    handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
    event: Message,
    data: dict[str, Any],
) -> Any:
    """Transcribes voice notes to text before any handler runs, so every
    flow below (FSM steps, find, free-form question) can just read
    message.text as usual — it never needs to know voice exists."""
    if event.voice is not None:
        try:
            bot: Bot = data["bot"]
            file = await bot.get_file(event.voice.file_id)
            buffer = await bot.download_file(file.file_path)
            text = await asyncio.to_thread(voice.transcribe, buffer.read())
        except Exception:
            # get_file/download_file aren't wrapped anywhere else — an
            # unhandled exception here used to propagate out of the
            # middleware entirely (aiogram just logs it, no reply to the
            # user at all: looked like "voice message ignored, nothing
            # happened"). Now it degrades to the same fallback as a failed
            # transcription instead of going silent.
            logger.exception("voice download/transcription pipeline failed")
            text = None
        if not text:
            await event.answer(
                "Не удалось распознать голосовое — попробуй ещё раз или напиши текстом."
            )
            return None
        event = event.model_copy(update={"text": text})
    return await handler(event, data)

_MAIN_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить эксперта/товар", callback_data="add")],
        [InlineKeyboardButton(text="🔍 Найти по категории", callback_data="find")],
    ]
)

_MAX_RESULTS = 3
_SUMMARY_LIMIT = 90  # one-line ad summary shown in search results, in chars
_MAX_TEXT_LEN = 1000  # cap on the "Добавить" flow's description field

_AGENT_SYSTEM_PROMPT = (
    "Ты — справочный помощник бота CariOra (база экспертов и услуг Бали). "
    "Тебе присылают список категорий, реально существующих в базе прямо "
    "сейчас, с количеством карточек в каждой, и вопрос пользователя. "
    "Отвечай ТОЛЬКО на основе присланного списка — никогда не выдумывай "
    "категории или числа, которых там нет. Если список пуст, так и скажи. "
    "Ты НЕ показываешь сами карточки (контакты, описания) — у тебя их "
    "просто нет, только список категорий с числами. Если вопрос касается "
    "существующей категории или похож на попытку что-то найти или "
    "добавить — всегда заканчивай ответ подсказкой нажать «🔍 Найти по "
    "категории» (чтобы увидеть сами карточки) или «➕ Добавить "
    "эксперта/товар». Отвечай кратко, по-русски, БЕЗ markdown-разметки — "
    "никаких **, _, `, только обычный текст, Telegram его не отрисует."
)

_agent_client: Optional[anthropic.Anthropic] = None


def _get_agent_client() -> anthropic.Anthropic:
    global _agent_client
    if _agent_client is None:
        _agent_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _agent_client


_BARE_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def _format_contact(value: str) -> str:
    """Telegram usernames sometimes arrive without the leading @ (scraper
    wrote sender.username raw, pre-fix rows; the bot's manual-add flow
    always prefixed it) — normalize every comma-separated piece so a bare
    username always shows with @. Links/phones/anything else pass through
    untouched (they don't match the username shape)."""
    parts = [p.strip() for p in value.split(",")]
    return ", ".join(
        f"@{p}" if p and not p.startswith("@") and _BARE_USERNAME_RE.match(p) else p
        for p in parts
    )


_CATEGORY_MATCH_SYSTEM_PROMPT = (
    "Тебе дают список категорий услуг, реально существующих в базе прямо "
    "сейчас, и поисковый запрос пользователя. Верни те категории — "
    "дословно из списка — которые подходят запросу ПО СМЫСЛУ, не только "
    "при точном совпадении слов. Например «нужна татуировка» должно "
    "найти категорию «татуировка» или «художественная аэрография» "
    "(близкая техника); «хочу постричься» — «услуги парикмахера»; «где "
    "выпить с музыкой» — «бар», «ресторан», «промоушн клубов». Не "
    "выдумывай категории, которых нет в списке. Если ничего разумно не "
    "подходит — верни пустой список. Отсортируй от самого релевантного к "
    "менее релевантному."
)

_CATEGORY_MATCH_TOOL_SCHEMA = {
    "name": "match_categories",
    "description": "Selects existing categories relevant to the user's search query, by meaning.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Categories from the input list, verbatim, relevant to "
                    "the query — most relevant first. Empty if nothing fits."
                ),
            }
        },
        "required": ["matched_categories"],
    },
}


def _match_categories_semantic(query: str, categories: list[str]) -> Optional[list[str]]:
    """AI-based category matching by meaning, not substring — e.g. "нужна
    татуировка" should find "татуировка" even without literal word overlap.
    Returns None (not []) on API failure so the caller can fall back to
    plain substring matching rather than showing "nothing found"."""
    if not categories:
        return []
    listing = "\n".join(f"- {c}" for c in categories)
    try:
        response = _get_agent_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=1000,
            system=_CATEGORY_MATCH_SYSTEM_PROMPT,
            tools=[_CATEGORY_MATCH_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "match_categories"},
            messages=[
                {
                    "role": "user",
                    "content": f"Категории:\n{listing}\n\nЗапрос пользователя: {query}",
                }
            ],
        )
    except Exception:
        # Broad on purpose: an SDK/transport-level failure (e.g. the
        # UnicodeEncodeError we hit in production building request headers)
        # isn't an anthropic.APIError and would otherwise propagate
        # unhandled — the whole point of this fallback is that search
        # never goes fully dark just because the AI call broke somehow.
        logger.exception("semantic category match failed, falling back to substring search")
        return None

    category_set = set(categories)
    matched = []
    for block in response.content:
        if block.type != "tool_use" or block.name != "match_categories":
            continue
        for cat in block.input.get("matched_categories", []):
            if cat in category_set and cat not in matched:
                matched.append(cat)
    return matched


def _one_line(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    """Deterministic fallback only (truncation, not a real summary) — used
    when _summarize_ad's Haiku call fails, so search never breaks."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


_AD_SUMMARY_SYSTEM_PROMPT = (
    "Сформулируй ОДНУ короткую строку (до 12 слов) о том, чем полезен этот "
    "эксперт или товар и что именно он делает — по тексту объявления. "
    "Только по-русски, без кавычек, без markdown, без вводных слов вроде "
    "«это объявление о» — сразу суть. Не копируй фразы из текста дословно, "
    "перескажи своими словами."
)


def _summarize_ad(text: str) -> str:
    """Real one-line summary via the cheapest model (Haiku), never a slice
    of the original text. Called only for the up-to-3 shown results, so
    cost stays bounded per search. Falls back to truncation on API error."""
    text = text.strip()
    if not text:
        return "—"
    try:
        response = _get_agent_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=60,
            system=_AD_SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
    except Exception:
        logger.exception("ad summary call failed, falling back to truncation")
        return _one_line(text)
    block = next((b for b in response.content if b.type == "text"), None)
    summary = block.text.strip() if block else ""
    return summary or _one_line(text)


def _pluralize_ru(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    n1 = n_abs % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def _feedback_note(positive: int, negative: int) -> str:
    """'✅ 1 положительный отзыв и ❌ 5 отрицательных отзывов', bold (HTML —
    caller's message uses parse_mode="HTML") — empty string if the card has
    no feedback yet (nothing shown in that case)."""
    parts = []
    if positive:
        adj = _pluralize_ru(positive, "положительный", "положительных", "положительных")
        noun = _pluralize_ru(positive, "отзыв", "отзыва", "отзывов")
        parts.append(f"✅ {positive} {adj} {noun}")
    if negative:
        adj = _pluralize_ru(negative, "отрицательный", "отрицательных", "отрицательных")
        noun = _pluralize_ru(negative, "отзыв", "отзыва", "отзывов")
        parts.append(f"❌ {negative} {adj} {noun}")
    if not parts:
        return ""
    return f"<b>{' и '.join(parts)}</b>"


_MARKDOWN_STRIP_RE = re.compile(r"(\*\*|__|[*_`#]+)")


def _strip_markdown(text: str) -> str:
    """Safety net for the freeform agent: the system prompt already says
    "no markdown", but LLMs don't always obey — strip the common markers
    so a stray ** doesn't show up literally (bot.answer() has no
    parse_mode here, so Telegram never renders them anyway)."""
    return _MARKDOWN_STRIP_RE.sub("", text)


def _is_low_credit_error(exc: anthropic.APIError) -> bool:
    """True for Anthropic's "credit balance too low" 400 — distinguished so
    the user gets told the real reason instead of a generic failure."""
    return (
        isinstance(exc, anthropic.BadRequestError)
        and "credit balance" in str(exc).lower()
    )


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _contact_key(card: dict) -> str:
    """Identity used to dedupe search results — prefers the stable
    "Ник" (Telegram username on the post) over "Дополнительные контакты",
    which can vary in formatting/extra links between reposts of the same
    offer by the same author. Empty string means "can't tell, don't dedupe
    this one against others"."""
    nickname = str(card.get("Ник", "")).strip().lower()
    if nickname:
        return nickname
    return str(card.get("Дополнительные контакты", "")).strip().lower()


def _match_score(category: str, query: str) -> int:
    """Higher = closer match to the query, for ranking search results so
    the best fits come first when more than _MAX_RESULTS qualify."""
    if category == query:
        return 2
    if category.startswith(query):
        return 1
    return 0


def _category_counts(cards: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for card in cards:
        category = str(card.get("Направление", "")).strip()
        if not category:
            continue
        counts[category] = counts.get(category, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


class AddCard(StatesGroup):
    category = State()
    name = State()
    text = State()
    confirm = State()


class FindCard(StatesGroup):
    category = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")]]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я CariOra — база экспертов и товаров.\n\n"
        "Могу добавить новую карточку в базу или найти по категории. "
        "Можно и просто спросить своими словами — например «какие категории уже есть?».",
        reply_markup=_MAIN_MENU,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=_MAIN_MENU)


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("editing"):
        # Cancel pressed mid single-field edit (from "✏️ Изменить") used to
        # wipe the WHOLE card back to the main menu — should only back out
        # of that one field, keeping everything already collected.
        await state.update_data(editing=False)
        await state.set_state(AddCard.confirm)
        text, markup = _confirm_view(data)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        return
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.message.answer("Что дальше?", reply_markup=_MAIN_MENU)
    await callback.answer()


# --- Добавить -----------------------------------------------------------


@router.callback_query(F.data == "add")
async def cb_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCard.category)
    await callback.message.edit_text(
        "Какая категория? (например: «психология», «аренда байков»)",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


def _confirm_view(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Renders the "check before saving" screen — shared by the initial
    pass through the flow and by every return trip from an edit."""
    text = (
        "Проверь карточку:\n\n"
        f"Категория: {data['category']}\n"
        f"Имя: {data['name']}\n"
        f"Описание: {data['text']}\n\n"
        "Контакт: возьмём твой Telegram-ник автоматически.\n\n"
        "Сохранить?"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сохранить", callback_data="save")],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="edit")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")],
        ]
    )
    return text, markup


async def _collect_field(
    message: Message,
    state: FSMContext,
    field: str,
    value: str,
    next_state: State,
    next_prompt: str,
) -> None:
    """Stores one field. If this was reached via the "✏️ Изменить" flow
    (data["editing"] set), jumps straight back to the confirm screen
    instead of continuing the linear category→name→text sequence."""
    prior = await state.get_data()
    editing = bool(prior.get("editing"))
    data = await state.update_data(**{field: value}, editing=False)
    if editing:
        await state.set_state(AddCard.confirm)
        text, markup = _confirm_view(data)
        await message.answer(text, reply_markup=markup)
    else:
        await state.set_state(next_state)
        await message.answer(next_prompt, reply_markup=_cancel_keyboard())


@router.message(AddCard.category)
async def add_category(message: Message, state: FSMContext) -> None:
    await _collect_field(
        message, state, "category", message.text.strip(),
        AddCard.name, "Имя и фамилия эксперта (или название товара/бренда)?",
    )


@router.message(AddCard.name)
async def add_name(message: Message, state: FSMContext) -> None:
    await _collect_field(
        message, state, "name", message.text.strip(),
        AddCard.text,
        f"Короткое описание услуги/товара — то, что увидят в поиске? "
        f"(до {_MAX_TEXT_LEN} символов)",
    )


@router.message(AddCard.text)
async def add_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if len(text) > _MAX_TEXT_LEN:
        await message.answer(
            f"Слишком длинно ({len(text)} символов, максимум {_MAX_TEXT_LEN}) — "
            "сократи и пришли ещё раз.",
            reply_markup=_cancel_keyboard(),
        )
        return
    # Always lands on the confirm screen regardless of the editing flag —
    # text is the last field in the linear order, so there's no "next
    # question" branch to take here, unlike category/name.
    data = await state.update_data(text=text, editing=False)
    await state.set_state(AddCard.confirm)
    view_text, markup = _confirm_view(data)
    await message.answer(view_text, reply_markup=markup)


_EDIT_FIELD_PROMPTS = {
    "category": ("Новая категория?", AddCard.category),
    "name": ("Новое имя/название?", AddCard.name),
    "text": (f"Новое описание (до {_MAX_TEXT_LEN} символов)?", AddCard.text),
}


@router.callback_query(AddCard.confirm, F.data == "edit")
async def cb_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "Что исправить?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Категория", callback_data="editfield:category")],
                [InlineKeyboardButton(text="Имя/название", callback_data="editfield:name")],
                [InlineKeyboardButton(text="Описание", callback_data="editfield:text")],
                [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(AddCard.confirm, F.data.startswith("editfield:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = (callback.data or "").split(":", 1)[1]
    entry = _EDIT_FIELD_PROMPTS.get(field)
    if entry is None:
        await callback.answer()
        return
    prompt, target_state = entry
    await state.update_data(editing=True)
    await state.set_state(target_state)
    await callback.message.edit_text(prompt, reply_markup=_cancel_keyboard())
    await callback.answer()


_FORBIDDEN_CHECK_SYSTEM_PROMPT = (
    "Проверь карточку эксперта/товара для CRM на запрещённые темы: "
    "наркотики и психоактивные вещества, эскорт/интим-услуги/проституция, "
    "оружие/боеприпасы/взрывчатка, поддельные документы, финансовые "
    "пирамиды и явное мошенничество, экстремистские материалы, услуги "
    "сексуального характера в отношении несовершеннолетних, торговля "
    "краденым, взлом систем/аккаунтов. Обычные легальные услуги (массаж, "
    "психология, бизнес-консультации, аренда, обучение, тантра, "
    "телесные практики и т.д.) НЕ запрещены, даже если тема деликатная — "
    "запрещай только явные, недвусмысленные случаи. Если сомневаешься — "
    "считай, что НЕ запрещено (пропускай пограничные случаи)."
)

_FORBIDDEN_CHECK_TOOL_SCHEMA = {
    "name": "check_forbidden_topic",
    "description": "Flags whether a service/product card touches a forbidden topic.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_forbidden": {"type": "boolean"},
            "reason": {
                "type": "string",
                "description": "Short Russian reason (a few words) if forbidden, empty string otherwise.",
            },
        },
        "required": ["is_forbidden", "reason"],
    },
}


def _check_forbidden_topic(category: str, name: str, text: str) -> Optional[str]:
    """Returns a short reason string if the card touches a forbidden
    topic, or None if it's fine OR the check itself failed. Fails open —
    a Haiku hiccup shouldn't block a legitimate submission, consistent
    with the "no moderation queue" design used everywhere else here."""
    content = f"Категория: {category}\nИмя/название: {name}\nОписание: {text}"
    try:
        response = _get_agent_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=200,
            system=_FORBIDDEN_CHECK_SYSTEM_PROMPT,
            tools=[_FORBIDDEN_CHECK_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "check_forbidden_topic"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        logger.exception("forbidden-topic check failed, allowing save (fail-open)")
        return None

    for block in response.content:
        if block.type == "tool_use" and block.name == "check_forbidden_topic":
            if block.input.get("is_forbidden"):
                return str(block.input.get("reason") or "запрещённая тема")
    return None


@router.callback_query(AddCard.confirm, F.data == "save")
async def add_save(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    username = callback.from_user.username or ""
    nickname = f"@{username}" if username else ""

    forbidden_reason = _check_forbidden_topic(data["category"], data["name"], data["text"])
    if forbidden_reason:
        await state.clear()
        await callback.message.edit_text(
            f"Не могу сохранить — похоже на запрещённую тему ({html.escape(forbidden_reason)}). "
            "Если это ошибка — переформулируй и добавь заново."
        )
        await callback.message.answer("Что дальше?", reply_markup=_MAIN_MENU)
        await callback.answer()
        return

    sheets_writer.append_card(
        nickname=nickname,
        first_name=data["name"],
        last_name="",
        category=data["category"],
        original_text=data["text"],
        extra_contacts="",
        published_at=dt.date.today().isoformat(),
    )
    await state.clear()
    await callback.message.edit_text("Сохранено ✅")
    await callback.message.answer("Что дальше?", reply_markup=_MAIN_MENU)
    await callback.answer()


# --- Найти ----------------------------------------------------------------


@router.callback_query(F.data == "find")
async def cb_find_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FindCard.category)
    await callback.message.edit_text(
        "Что ищем? Можно не точным названием категории, а своими словами "
        "(например: «нужна татуировка», «где постричься», «хочу на вечеринку»)",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.message(FindCard.category)
async def find_category(message: Message, state: FSMContext) -> None:
    await state.clear()
    query = message.text.strip().lower()
    if not query:
        await message.answer("Пустой запрос, попробуй ещё раз.", reply_markup=_MAIN_MENU)
        return

    try:
        cards = sheets_writer.list_cards_with_rows()
    except Exception:
        logger.exception("failed to read cards from sheet")
        await message.answer(
            "Не получилось прочитать базу, попробуй чуть позже.", reply_markup=_MAIN_MENU
        )
        return

    categories = sorted({str(c.get("Направление", "")).strip() for c in cards if str(c.get("Направление", "")).strip()})
    matched_categories = _match_categories_semantic(message.text.strip(), categories)

    if matched_categories is not None:
        # AI match by meaning — matches list is already in relevance order.
        rank = {cat: i for i, cat in enumerate(matched_categories)}
        matches = [c for c in cards if str(c.get("Направление", "")).strip() in rank]
        matches.sort(key=lambda c: rank[str(c.get("Направление", "")).strip()])
    else:
        # AI call failed (e.g. no API credit) — fall back to substring
        # search on the category text rather than showing nothing.
        matches = [c for c in cards if query in str(c.get("Направление", "")).lower()]
        matches.sort(key=lambda c: -_match_score(str(c.get("Направление", "")).lower(), query))

    if not matches:
        await message.answer(
            f"По запросу «{message.text.strip()}» пока ничего нет.", reply_markup=_MAIN_MENU
        )
        return
    # Same author reposting the same offer (dupes not yet cleaned from the
    # sheet, or dedup gaps) shouldn't eat multiple result slots — one card
    # per contact, keeping the best-ranked (already sorted above).
    seen_contacts: set[str] = set()
    deduped = []
    for card in matches:
        key = _contact_key(card)
        if key and key in seen_contacts:
            continue
        if key:
            seen_contacts.add(key)
        deduped.append(card)
    matches = deduped
    shown = matches[:_MAX_RESULTS]

    lines = [f"<b>Найдено: {len(matches)}</b>", ""]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for i, card in enumerate(shown, start=1):
        name = f"{card.get('Имя', '')} {card.get('Фамилия', '')}".strip() or str(
            card.get("Ник") or "без имени"
        )
        category = str(card.get("Направление", ""))
        summary = _summarize_ad(str(card.get("Оригинальный текст объявления", "")))
        contact_raw = str(card.get("Дополнительные контакты") or card.get("Ник") or "—")
        contact = _format_contact(contact_raw)

        line = (
            f"{i}. <b>{html.escape(name)}</b> ({html.escape(category)}) — "
            f"{html.escape(summary)} — {html.escape(str(contact))}"
        )
        note = _feedback_note(_as_int(card.get("Отзывы +")), _as_int(card.get("Отзывы -")))
        if note:
            line += f"\n   {note}"
        lines.append(line)

        row = card["_row"]
        keyboard_rows.append(
            [
                InlineKeyboardButton(text=f"👍 {i}", callback_data=f"fb:{row}:pos"),
                InlineKeyboardButton(text=f"👎 {i}", callback_data=f"fb:{row}:neg"),
            ]
        )

    if len(matches) > len(shown):
        lines.append("")
        lines.append(f"Показаны {len(shown)} наиболее подходящих.")

    # Feedback buttons alone left a dead end if none of the 3 results fit —
    # always offer a fresh search / add right under them.
    keyboard_rows.extend(_MAIN_MENU.inline_keyboard)

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


# --- Отзыв на результат поиска ----------------------------------------------


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("fb:"))
async def cb_feedback(callback: CallbackQuery) -> None:
    try:
        _, row_str, kind = (callback.data or "").split(":")
        row = int(row_str)
    except ValueError:
        await callback.answer()
        return

    try:
        sheets_writer.add_feedback(row, positive=kind == "pos")
    except Exception:
        logger.exception("failed to record feedback")
        await callback.answer("Не получилось сохранить отзыв, попробуй позже.", show_alert=True)
        return

    markup = callback.message.reply_markup
    if markup:
        new_rows = []
        for keyboard_row in markup.inline_keyboard:
            if keyboard_row and (keyboard_row[0].callback_data or "").startswith(f"fb:{row}:"):
                new_rows.append(
                    [InlineKeyboardButton(text="Отзыв учтён ✅", callback_data="noop")]
                )
            else:
                new_rows.append(keyboard_row)
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows)
        )
    await callback.answer("Спасибо за отзыв!")


# --- Свободный вопрос → ИИ-агент ---------------------------------------------


@router.message(StateFilter(None))
async def freeform_question(message: Message) -> None:
    """Anything typed outside the add/find FSM and not a known command lands
    here. Grounded in the real category list so it can't invent data; never
    performs the actual search/write itself (those stay in the deterministic
    handlers above)."""
    if not message.text:
        return

    try:
        cards = sheets_writer.list_cards()
    except Exception:
        logger.exception("failed to read cards for freeform agent")
        await message.answer(
            "Не получилось прочитать базу, попробуй чуть позже.", reply_markup=_MAIN_MENU
        )
        return

    counts = _category_counts(cards)
    categories_text = (
        "\n".join(f"- {cat}: {n}" for cat, n in counts) if counts else "(пока пусто)"
    )

    try:
        response = _get_agent_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=400,
            system=_AGENT_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Категории в базе сейчас:\n{categories_text}\n\n"
                        f"Вопрос пользователя: {message.text}"
                    ),
                }
            ],
        )
    except Exception as exc:
        logger.exception("freeform agent call failed")
        # A dead/out-of-credit key looks identical to the user as any other
        # failure otherwise — say plainly what's wrong instead of the old
        # "не разобрал вопрос", which reads as if the model tried and just
        # didn't understand a word like "разработчик". Also point at the
        # deterministic search, since that's what most free-form messages
        # actually want and it doesn't depend on the AI agent at all.
        if _is_low_credit_error(exc):
            text = (
                "ИИ-помощник сейчас недоступен — закончились токены Anthropic "
                "(это не про твой вопрос, чинится пополнением баланса).\n\n"
                "Ищешь что-то конкретное? Жми «🔍 Найти по категории» — "
                "это работает без ИИ."
            )
        else:
            text = "Не получилось обработать вопрос — попробуй ещё раз чуть позже."
        await message.answer(text, reply_markup=_MAIN_MENU)
        return

    text_block = next((b for b in response.content if b.type == "text"), None)
    reply = text_block.text if text_block else "Не разобрал вопрос — жми /start."
    await message.answer(_strip_markdown(reply), reply_markup=_MAIN_MENU)


async def run() -> None:
    """Runs polling forever. aiogram already retries on ordinary network
    blips inside start_polling, but a longer outage (e.g. the Mac sleeping
    for hours) can leave it in a state it won't recover from on its own —
    so this wraps it in a restart loop as a second line of defense. The
    real fix for staying up while the machine sleeps is the launchd
    service (see README) — this loop only protects against the process
    itself getting stuck while the machine IS awake."""
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    while True:
        try:
            await dp.start_polling(bot)
        except Exception:
            logger.exception("polling crashed, restarting in 5s")
            await asyncio.sleep(5)
