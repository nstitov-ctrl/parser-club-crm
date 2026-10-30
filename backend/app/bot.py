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
import secrets
import time
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

from app import sheets_writer, voice, votes
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
        text = ""
        error_code = None
        try:
            bot: Bot = data["bot"]
            file = await bot.get_file(event.voice.file_id)
            buffer = await bot.download_file(file.file_path)
            text = await asyncio.to_thread(voice.transcribe, buffer.read())
        except voice.VoiceError as exc:
            error_code = exc.code
        except Exception as exc:
            # get_file/download_file aren't wrapped anywhere else — an
            # unhandled exception here used to propagate out of the
            # middleware entirely (aiogram just logs it, no reply to the
            # user at all: looked like "voice message ignored, nothing
            # happened"). Now it degrades to a reply instead of going silent.
            logger.exception("voice download pipeline failed")
            error_code = f"download:{type(exc).__name__}"
        if error_code:
            await event.answer(
                "Голосовой ввод сейчас не работает — напиши текстом, пожалуйста. "
                f"(код: {error_code})"
            )
            return None
        if not text:
            await event.answer(
                "Не расслышал — скажи чуть громче и подольше или напиши текстом."
            )
            return None
        event = event.model_copy(update={"text": text})
    return await handler(event, data)

_MAIN_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cabinet")],
        [InlineKeyboardButton(text="🔍 Найти по категории", callback_data="find")],
    ]
)

_WELCOME_TEXT = (
    "Привет! 👋 Я CariOra.\n\n"
    "Я знаю на Бали очень много полезных людей и помогу тебе найти именно того, кто тебе нужен.\n"
    "Просто нажми на кнопку 🎤 и расскажи голосом, что ты ищешь.\n"
    "Хочешь сделать татуировку? Купить недвижимость? Найти специалиста? "
    "Или просто душевно с кем-то поговорить?\n"
    "Скажи мне, чего ты хочешь, — и я помогу найти твоего человека.\n"
    "А если ты хочешь, чтобы нашли тебя, хочешь заявить о себе на этом острове — "
    "нажми кнопку «Личный кабинет» и расскажи о себе.\n\n"
    "CariOra. Люди находят людей. ❤️"
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
    "категории» (чтобы увидеть сами карточки) или «👤 Личный "
    "кабинет» (чтобы рассказать о себе). Отвечай кратко, по-русски, БЕЗ markdown-разметки — "
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
    "выпить с музыкой» — «бар», «ресторан», «промоушн клубов».\n\n"
    "БУДЬ СТРОГИМ: категория должна закрывать именно ту потребность, что "
    "в запросе, а не просто быть из той же widely-related сферы бизнеса. "
    "«Специалисты по ресторанному бизнесу» — это НЕ «маркетинг и "
    "брендинг» и НЕ «бухгалтерское сопровождение» просто потому что это "
    "тоже про бизнес — эти категории обслуживают ЛЮБОЙ бизнес, а не "
    "именно рестораны, так что они НЕ подходят, если явно не упомянуты в "
    "запросе. Если в списке есть категория конкретно про рестораны/кафе "
    "— бери её, а не общие бизнес-услуги.\n\n"
    "Правило: НЕ включай общую/универсальную категорию (бухгалтерия, "
    "юрист, маркетинг, консалтинг и т.п.) в ответ на нишевый запрос "
    "(конкретная профессия/услуга/индустрия), если только в запросе "
    "явно не спрашивают именно про неё. Лучше вернуть пустой список или "
    "одну категорию, чем 10 formally-adjacent — точность важнее охвата. "
    "Не выдумывай категории, которых нет в списке. Отсортируй от самого "
    "релевантного к менее релевантному."
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
            temperature=0,  # deterministic, strict matching — default (1.0)
            # sampling let it occasionally include loosely-related
            # categories (a tattoo query once returned "графический
            # дизайн"), varying between otherwise-identical calls.
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


class EditMine(StatesGroup):
    value = State()  # waiting for the new value of one field of the author's own card


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")]]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_WELCOME_TEXT, reply_markup=_MAIN_MENU)


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
            temperature=0,  # consistent classification, not a creative task
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

    if username and await asyncio.to_thread(
        sheets_writer.has_card_in_category, username, data["category"]
    ):
        await state.clear()
        await callback.message.edit_text(
            "У тебя уже есть объявление в этой категории. Открой «Личный кабинет» и измени его."
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
        author_id=callback.from_user.id,  # stable id: self-vote check survives a username change
        confirmed_on=dt.date.today().isoformat(),  # the author wrote it themselves → verified at once
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
    await _search_and_reply(message, state)


async def _search_and_reply(
    message: Message,
    state: FSMContext,
    no_match: Optional[Callable[[Message], Awaitable[None]]] = None,
) -> None:
    """The search itself. `no_match`, when given, handles an empty result instead
    of the plain "ничего нет" — free-form messages use it to fall back to the
    helper agent."""
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

    rank: Optional[dict[str, int]] = None
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
        if no_match is not None:
            await no_match(message)
            return
        await message.answer(
            f"По запросу «{message.text.strip()}» пока ничего нет.", reply_markup=_MAIN_MENU
        )
        return
    # Best-rated first: biggest (👍 − 👎), then more 👍; search relevance only
    # breaks ties. Done BEFORE the per-contact dedupe below so the best copy
    # of a repost is the one that survives.
    matches = votes.sort_by_rating(matches, rank=rank)
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

    # Remember the whole ordered list so "Показать ещё" can continue it later.
    token = _remember_search([votes.card_id(c) for c in matches])
    text, keyboard = _results_page(matches[:_MAX_RESULTS], 0, len(matches), token)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


# --- Выдача порциями: 3 карточки + «Показать ещё» ---------------------------

# callback_data is capped at 64 bytes, so a search's ordered card ids live in
# memory under a short token. Entries expire; a bot restart simply asks the
# user to repeat the search.
_SEARCH_CACHE: dict[str, tuple[float, list[str]]] = {}
_SEARCH_TTL = 3600
_SEARCH_CACHE_MAX = 500


def _remember_search(card_ids: list[str]) -> str:
    now = time.time()
    for key in [k for k, (ts, _) in _SEARCH_CACHE.items() if now - ts > _SEARCH_TTL]:
        del _SEARCH_CACHE[key]
    while len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:             # drop the oldest
        del _SEARCH_CACHE[min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])]
    token = secrets.token_hex(4)
    _SEARCH_CACHE[token] = (now, card_ids)
    return token


def _results_page(
    page: list[dict], start: int, total: int, token: str, next_offset: Optional[int] = None
) -> tuple[str, InlineKeyboardMarkup]:
    """One message: up to _MAX_RESULTS cards numbered from start+1, their
    buttons, and — if more remain — "Показать ещё N" under them.
    next_offset is where the following page starts in the full list (differs
    from start+len(page) when a card vanished from the sheet in between)."""
    if start == 0:
        lines = [f"<b>Найдено: {total}</b>", ""]
    else:
        lines = [f"<b>Ещё результаты: {start + 1}–{start + len(page)} из {total}</b>", ""]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for i, card in enumerate(page, start=start + 1):
        name = f"{card.get('Имя', '')} {card.get('Фамилия', '')}".strip() or str(
            card.get("Ник") or "без имени"
        )
        category = str(card.get("Направление", ""))
        summary = _summarize_ad(votes.display_text(card))   # the author's corrected text wins
        contact_raw = str(card.get("Дополнительные контакты") or card.get("Ник") or "—")
        contact = _format_contact(contact_raw)
        badge = " ✅" if votes.is_verified(card) else ""     # the author confirmed the card (≤ 6 months ago)

        line = (
            f"{i}. <b>{html.escape(name)}</b>{badge} ({html.escape(category)}) — "
            f"{html.escape(summary)} — {html.escape(str(contact))}"
        )
        note = _feedback_note(_as_int(card.get("Отзывы +")), _as_int(card.get("Отзывы -")))
        if note:
            line += f"\n   {note}"
        lines.append(line)

        # Cards are addressed by a stable id (author+category), not a sheet row:
        # rows shift whenever someone deletes rows, which would credit the
        # wrong card. A vote is only accepted after "Написать" was pressed.
        cid = votes.card_id(card)
        keyboard_rows.append(
            [
                InlineKeyboardButton(text=f"✉️ Написать {i}", callback_data=f"ct:{cid}"),
                InlineKeyboardButton(text=f"👍 {i}", callback_data=f"fb:{cid}:pos"),
                InlineKeyboardButton(text=f"👎 {i}", callback_data=f"fb:{cid}:neg"),
            ]
        )

    if next_offset is None:
        next_offset = start + len(page)
    if next_offset < total:
        n = min(_MAX_RESULTS, total - next_offset)
        keyboard_rows.append(
            [InlineKeyboardButton(text=f"⬇️ Показать ещё {n}", callback_data=f"more:{token}:{next_offset}")]
        )

    # Feedback buttons alone left a dead end if none of the results fit —
    # always offer a fresh search / add right under them.
    keyboard_rows.extend(_MAIN_MENU.inline_keyboard)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@router.callback_query(F.data.startswith("more:"))
async def cb_more(callback: CallbackQuery) -> None:
    try:
        _, token, offset_str = (callback.data or "").split(":")
        offset = int(offset_str)
    except ValueError:
        await callback.answer()
        return
    entry = _SEARCH_CACHE.get(token)
    if entry is None or time.time() - entry[0] > _SEARCH_TTL or offset < 0:
        await callback.answer("Список устарел — повтори поиск.", show_alert=True)
        return
    ids = entry[1]

    # Take the button off the message first, so a double tap can't post the page twice.
    markup = callback.message.reply_markup
    if markup:
        rows = [r for r in markup.inline_keyboard
                if not any((b.callback_data or "").startswith("more:") for b in r)]
        try:
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        except Exception:
            logger.debug("could not drop 'more' button", exc_info=True)

    try:
        cards = await asyncio.to_thread(sheets_writer.list_cards_with_rows)
    except Exception:
        logger.exception("failed to read cards for 'more'")
        await callback.answer("Не получилось прочитать базу, попробуй чуть позже.", show_alert=True)
        return
    by_id = {votes.card_id(c): c for c in cards}                 # fresh ratings; deleted cards drop out
    page = [by_id[i] for i in ids[offset:offset + _MAX_RESULTS] if i in by_id]
    if not page:
        await callback.answer("Больше результатов нет.")
        return
    text, keyboard = _results_page(page, offset, len(ids), token, next_offset=offset + _MAX_RESULTS)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


# --- Отзыв на результат поиска ----------------------------------------------


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


_CARD_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _tg_user(callback: CallbackQuery) -> dict:
    return {"id": callback.from_user.id, "username": callback.from_user.username}


def _find_card(cards: list[dict], cid: str) -> Optional[dict]:
    return next((c for c in cards if votes.card_id(c) == cid), None)


@router.callback_query(F.data.startswith("ct:"))
async def cb_contact(callback: CallbackQuery) -> None:
    """"✉️ Написать" — shows how to reach the author AND records the contact,
    which is what unlocks rating that card (see app/votes.py)."""
    cid = (callback.data or "")[3:]
    if not _CARD_ID_RE.match(cid):
        await callback.answer("Кнопка устарела — повтори поиск.", show_alert=True)
        return
    try:
        cards = await asyncio.to_thread(sheets_writer.list_cards_with_rows)
        card = _find_card(cards, cid)
        if card is None:
            await callback.answer("Карточка изменилась — повтори поиск.", show_alert=True)
            return
        await asyncio.to_thread(sheets_writer.get_ledger().record_contact, card, _tg_user(callback))
    except Exception:
        logger.exception("failed to record contact")
        await callback.answer("Не получилось открыть контакт, попробуй позже.", show_alert=True)
        return

    nick = str(card.get("Ник") or "").strip().lstrip("@")
    contact = _format_contact(str(card.get("Дополнительные контакты") or card.get("Ник") or "—"))
    name = f"{card.get('Имя', '')} {card.get('Фамилия', '')}".strip() or nick or "исполнитель"
    link = f'<a href="https://t.me/{nick}">@{html.escape(nick)}</a>\n' if nick else ""
    await callback.message.answer(
        f"✉️ <b>{html.escape(name)}</b>\n{link}{html.escape(contact)}\n\n"
        "Когда свяжетесь — можно оставить оценку 👍/👎 в списке выше.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fb:"))
async def cb_feedback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[2] not in ("pos", "neg") or not _CARD_ID_RE.match(parts[1]):
        # also catches buttons from messages sent before the ledger existed (fb:<row>:..)
        await callback.answer("Кнопка устарела — повтори поиск.", show_alert=True)
        return
    cid, positive = parts[1], parts[2] == "pos"

    try:
        cards = await asyncio.to_thread(sheets_writer.list_cards_with_rows)
        result = await asyncio.to_thread(
            sheets_writer.get_ledger().cast_vote, cards, cid, _tg_user(callback), positive
        )
    except votes.VoteError as err:
        await callback.answer(err.message, show_alert=True)
        return
    except Exception:
        logger.exception("failed to record feedback")
        await callback.answer("Не получилось сохранить оценку, попробуй позже.", show_alert=True)
        return

    # Mark the standing vote on this card's buttons (it can still be changed).
    markup = callback.message.reply_markup
    if markup:
        new_rows = []
        for keyboard_row in markup.inline_keyboard:
            if any((b.callback_data or "").startswith(f"fb:{cid}:") for b in keyboard_row):
                fixed = []
                for b in keyboard_row:
                    if (b.callback_data or "").startswith(f"fb:{cid}:"):
                        mine = b.callback_data.endswith(":pos" if positive else ":neg")
                        label = b.text.removeprefix("✅ ")
                        b = InlineKeyboardButton(text=("✅ " + label) if mine else label,
                                                 callback_data=b.callback_data)
                    fixed.append(b)
                new_rows.append(fixed)
            else:
                new_rows.append(keyboard_row)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows)
            )
        except Exception:
            logger.debug("reply markup unchanged", exc_info=True)

    if result["changed"]:
        await callback.answer(f"Оценка учтена: 👍 {result['pos']} · 👎 {result['neg']}")
    else:
        await callback.answer("Эта оценка у вас уже стоит.")


# --- Личный кабинет: объявления автора, подтверждение и правка по @нику ----------------------------
#
# Whoever writes to the bot from the @username on a card is treated as its author
# (verification is by nick only). Confirming or correcting a card stamps today's
# date in "Подтверждено" — search then shows a ✅ next to the name for ~6 months,
# and a corrected text is shown INSTEAD of the scraped one.

_MY_CARDS_MAX = 8
_EDIT_FIELDS = {  # callback field -> (sheet column, question)
    "name": ("Имя", "Новое имя или название?"),
    "category": ("Направление", "Новая категория? (например: «психология», «аренда байков»)"),
    "text": (
        votes.AUTHOR_TEXT_COL,
        f"Новое описание — его увидят в поиске вместо исходного (до {_MAX_TEXT_LEN} символов)?",
    ),
}


def _mine_buttons(cid: str) -> InlineKeyboardMarkup:
    """Under each card in the cabinet: confirm as-is, or open the "what to change" choice."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"vf:{cid}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ed:{cid}"),
            ]
        ]
    )


def _mine_edit_buttons(cid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Имя", callback_data=f"em:{cid}:name"),
                InlineKeyboardButton(text="📂 Категория", callback_data=f"em:{cid}:category"),
                InlineKeyboardButton(text="📝 Текст", callback_data=f"em:{cid}:text"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить объявление", callback_data=f"dl:{cid}")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data=f"bk:{cid}")],
        ]
    )


def _cabinet_footer() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать новую", callback_data="add")],
            [InlineKeyboardButton(text="🔍 Найти по категории", callback_data="find")],
        ]
    )


def _mine_card_view(card: dict) -> tuple[str, InlineKeyboardMarkup]:
    cid = votes.card_id(card)
    name = f"{card.get('Имя', '')} {card.get('Фамилия', '')}".strip() or str(card.get("Ник") or "без имени")
    snippet = " ".join(votes.display_text(card).split())
    if len(snippet) > 280:
        snippet = snippet[:280].rstrip() + "…"
    status = "✅ подтверждена" if votes.is_verified(card) else "не подтверждена"
    text = (
        f"<b>{html.escape(name)}</b> ({html.escape(str(card.get('Направление', '')))})\n"
        f"{html.escape(snippet)}\n\nСтатус: {status}"
    )
    return text, _mine_buttons(cid)


@router.callback_query(F.data.in_({"cabinet", "mine"}))  # "mine" = buttons in messages sent before the rename
async def cb_cabinet(callback: CallbackQuery, state: FSMContext) -> None:
    """Личный кабинет: the ads already added under this @nick (each with
    "Изменить"), and below them "Создать новую"."""
    await state.clear()
    username = callback.from_user.username
    if not username:
        await callback.message.answer(
            "👤 <b>Личный кабинет</b>\n\nУ тебя в Telegram не указан username (@ник): объявления "
            "привязываются к нику, поэтому показать уже добавленные не получится. Укажи ник в настройках "
            "Telegram и вернись сюда. Новое объявление можно создать и сейчас.",
            parse_mode="HTML",
            reply_markup=_cabinet_footer(),
        )
        await callback.answer()
        return
    try:
        cards = await asyncio.to_thread(sheets_writer.find_my_cards, username)
    except Exception:
        logger.exception("failed to load author's cards")
        await callback.answer("Не получилось прочитать базу, попробуй чуть позже.", show_alert=True)
        return
    who = f"@{html.escape(username)}"
    if not cards:
        await callback.message.answer(
            f"👤 <b>Личный кабинет</b> · {who}\n\nОбъявлений с этим ником пока нет.",
            parse_mode="HTML",
            reply_markup=_cabinet_footer(),
        )
        await callback.answer()
        return
    await callback.message.answer(
        f"👤 <b>Личный кабинет</b> · {who}\nВаши объявления: {len(cards)}", parse_mode="HTML"
    )
    for card in cards[:_MY_CARDS_MAX]:
        text, markup = _mine_card_view(card)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    if len(cards) > _MY_CARDS_MAX:
        await callback.message.answer(f"…и ещё {len(cards) - _MY_CARDS_MAX}.")
    await callback.message.answer("Что дальше?", reply_markup=_cabinet_footer())
    await callback.answer()


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit_menu(callback: CallbackQuery) -> None:
    """"✏️ Изменить" — swaps the card's buttons for the choice of what to change."""
    cid = (callback.data or "")[3:]
    if not _CARD_ID_RE.match(cid):
        await callback.answer("Кнопка устарела — открой «Личный кабинет» заново.", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=_mine_edit_buttons(cid))
    except Exception:
        logger.debug("edit menu unchanged", exc_info=True)
    await callback.answer("Что изменить?")


@router.callback_query(F.data.startswith("dl:"))
async def cb_delete_ask(callback: CallbackQuery) -> None:
    """"🗑 Удалить объявление" — asks for confirmation before anything is removed."""
    cid = (callback.data or "")[3:]
    if not _CARD_ID_RE.match(cid):
        await callback.answer("Кнопка устарела — открой «Личный кабинет» заново.", show_alert=True)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"dy:{cid}")],
            [InlineKeyboardButton(text="↩️ Нет, оставить", callback_data=f"bk:{cid}")],
        ]
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.debug("delete confirmation unchanged", exc_info=True)
    await callback.answer("Удалить объявление? Оно пропадёт из поиска.")


@router.callback_query(F.data.startswith("dy:"))
async def cb_delete_do(callback: CallbackQuery) -> None:
    cid = (callback.data or "")[3:]
    if not _CARD_ID_RE.match(cid):
        await callback.answer("Кнопка устарела — открой «Личный кабинет» заново.", show_alert=True)
        return
    try:
        await asyncio.to_thread(sheets_writer.delete_my_card, cid, callback.from_user.username)
    except sheets_writer.CardUpdateError as err:
        await callback.answer(str(err), show_alert=True)
        return
    except Exception:
        logger.exception("failed to delete card")
        await callback.answer("Не получилось удалить, попробуй позже.", show_alert=True)
        return
    try:
        await callback.message.edit_text("🗑 Объявление удалено.")
    except Exception:
        logger.debug("delete message unchanged", exc_info=True)
    await callback.answer("Объявление удалено")


@router.callback_query(F.data.startswith("bk:"))
async def cb_edit_back(callback: CallbackQuery) -> None:
    cid = (callback.data or "")[3:]
    if not _CARD_ID_RE.match(cid):
        await callback.answer()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=_mine_buttons(cid))
    except Exception:
        logger.debug("back button unchanged", exc_info=True)
    await callback.answer()


@router.callback_query(F.data.startswith("vf:"))
async def cb_confirm_mine(callback: CallbackQuery) -> None:
    cid = (callback.data or "")[3:]
    if not _CARD_ID_RE.match(cid):
        await callback.answer("Кнопка устарела — открой «Личный кабинет» заново.", show_alert=True)
        return
    try:
        card = await asyncio.to_thread(
            sheets_writer.update_my_card, cid, callback.from_user.username, {}
        )
    except sheets_writer.CardUpdateError as err:
        await callback.answer(str(err), show_alert=True)
        return
    except Exception:
        logger.exception("failed to confirm card")
        await callback.answer("Не получилось сохранить, попробуй позже.", show_alert=True)
        return
    text, markup = _mine_card_view(card)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        logger.debug("card view unchanged", exc_info=True)
    await callback.answer("Карточка подтверждена ✅")


@router.callback_query(F.data.startswith("em:"))
async def cb_edit_mine(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[2] not in _EDIT_FIELDS or not _CARD_ID_RE.match(parts[1]):
        await callback.answer("Кнопка устарела — открой «Личный кабинет» заново.", show_alert=True)
        return
    cid, field = parts[1], parts[2]
    try:
        cards = await asyncio.to_thread(sheets_writer.find_my_cards, callback.from_user.username)
    except Exception:
        logger.exception("failed to load author's cards")
        await callback.answer("Не получилось прочитать базу, попробуй чуть позже.", show_alert=True)
        return
    if not any(votes.card_id(c) == cid for c in cards):
        await callback.answer("Эта карточка привязана к другому нику или изменилась.", show_alert=True)
        return
    await state.set_state(EditMine.value)
    await state.update_data(cid=cid, field=field)
    await callback.message.answer(_EDIT_FIELDS[field][1], reply_markup=_cancel_keyboard())
    await callback.answer()


@router.message(EditMine.value)
async def mine_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid, field = data.get("cid", ""), data.get("field", "")
    value = (message.text or "").strip()
    if field not in _EDIT_FIELDS or not value:
        await message.answer("Пришли новое значение текстом.", reply_markup=_cancel_keyboard())
        return
    if field == "text" and len(value) > _MAX_TEXT_LEN:
        await message.answer(
            f"Слишком длинно ({len(value)} символов, максимум {_MAX_TEXT_LEN}) — сократи и пришли ещё раз.",
            reply_markup=_cancel_keyboard(),
        )
        return
    column = _EDIT_FIELDS[field][0]
    if field == "category":
        value = sheets_writer.normalize_category(value)
    username = message.from_user.username
    try:
        cards = await asyncio.to_thread(sheets_writer.find_my_cards, username)
        card = next((c for c in cards if votes.card_id(c) == cid), None)
        if card is None:
            raise sheets_writer.CardUpdateError("Карточка изменилась — открой «Личный кабинет» заново.")
        reason = await asyncio.to_thread(
            _check_forbidden_topic,
            value if field == "category" else str(card.get("Направление", "")),
            value if field == "name" else str(card.get("Имя") or card.get("Ник") or ""),
            value if field == "text" else votes.display_text(card),
        )
        if reason:
            await message.answer(
                f"Не могу сохранить — похоже на запрещённую тему ({html.escape(reason)}). "
                "Переформулируй и пришли ещё раз.",
                reply_markup=_cancel_keyboard(),
            )
            return
        updated = await asyncio.to_thread(sheets_writer.update_my_card, cid, username, {column: value})
    except sheets_writer.CardUpdateError as err:
        await state.clear()
        await message.answer(str(err), reply_markup=_MAIN_MENU)
        return
    except Exception:
        logger.exception("failed to update author's card")
        await state.clear()
        await message.answer("Не получилось сохранить, попробуй позже.", reply_markup=_MAIN_MENU)
        return
    await state.clear()
    await message.answer("Сохранено ✅ Карточка подтверждена — в поиске рядом с ней теперь галочка.")
    view, markup = _mine_card_view(updated)
    await message.answer(view, parse_mode="HTML", reply_markup=markup)
    await message.answer("Что дальше?", reply_markup=_MAIN_MENU)


# --- Свободный вопрос → ИИ-агент ---------------------------------------------


@router.message(StateFilter(None))
async def freeform_question(message: Message, state: FSMContext) -> None:
    """Anything typed or said outside the add/find FSM and not a known command.
    The welcome message promises "tell me what you need and I'll find your
    person", so it is first treated as a search request; only when nothing
    matches does the helper agent answer."""
    if not message.text:
        return
    await _search_and_reply(message, state, no_match=_agent_answer)


async def _agent_answer(message: Message) -> None:
    """Helper agent for messages that found nothing. Grounded in the real
    category list so it can't invent data; never shows cards itself (the
    deterministic search above does that)."""
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
    # Load the speech model in the background so the first voice note after a
    # (re)start doesn't wait for it; polling starts immediately.
    asyncio.create_task(asyncio.to_thread(voice.preload))
    while True:
        try:
            await dp.start_polling(bot)
        except Exception:
            logger.exception("polling crashed, restarting in 5s")
            await asyncio.sleep(5)
