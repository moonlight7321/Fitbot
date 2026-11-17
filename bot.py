"""Точка входа и хендлеры aiogram 3.x.

Пайплайн одного поста:
1) DeepSeek генерирует три блока: [ПОСТ], [ИДЕЯ ДЛЯ КАРТИНКИ], [ПРОВЕРКА];
2) пост независимо проверяется вторым запросом, при замечаниях — автоисправление;
3) картинка от Pollinations скачивается параллельно с проверкой;
4) результат уходит в чат, откуда пришла команда, плюс кнопка «Опубликовать в канал».

Автопостинг (scheduler.py) использует тот же пайплайн и публикует пост в канал
только если проверка не нашла серьёзных проблем.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Union
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    InputMediaPhoto,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    WebAppInfo,
    BufferedInputFile,
    FSInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from ai_client import AIError, DeepSeekClient, check_length, clean_post, parse_sections
from config import Settings, load_settings
from image_gen import fetch_image, log_build_info
from scheduler import create_scheduler, describe_schedule
import backup
import calories
import diary
import exercises
import orders
import progress
import planner
import profiles
import subscriptions as subs
import webapp_api

logger = logging.getLogger(__name__)

router = Router()          # админские команды, закрыты OwnerOnlyMiddleware
pay_router = Router()      # /sub и оплата — для всех пользователей

CAPTION_LIMIT = 1024   # лимит подписи к фото в Telegram
TEXT_LIMIT = 4000      # лимит текстового сообщения
PENDING_LIMIT = 50     # сколько черновиков держим для кнопки «Опубликовать»

# Чередование тем — ровно список из технического задания
TOPIC_ROTATION: List[str] = [
    "Короткая тренировка на 15–30 минут (можно без оборудования или с минимумом)",
    "Простой приём для улучшения рациона: замена продукта, размер порции, планирование",
    "Восстановление и сон как часть прогресса",
    "полноценная тренировка на 45–60 минут (с разминкой, основной частью и заминкой, по блокам с упражнениями, подходами и повторениями)",
    "полноценная тренировка на 45–60 минут для дома без оборудования",
    "полноценная тренировка на конкретную группу мышц: ноги, спина, грудь или плечи (выбери сама)",
    "экспресс-тренировка на 15–20 минут для занятых",
    "полезный лайфхак или совет по восстановлению, питанию или технике упражнения",
    "мини-чек-лист из 5–7 пунктов, например «как правильно разминаться» или «частые ошибки в приседаниях»",
    "мотивационный пост с конкретным советом, а не просто цитатой",
    "Разбор популярного мифа о спорте или питании",
    "Мотивация и микрошаг на сегодня, который реально сделать в плотном графике",
]


@dataclass
class GeneratedContent:
    """Всё, что нужно, чтобы показать и опубликовать один пост."""

    topic: str
    post: str
    image_prompt: str
    review_text: str
    review_ok: bool
    image: Optional[bytes] = None
    notes: List[str] = field(default_factory=list)


class TopicRotation:
    """По очереди выдаёт темы.

    Индекс живёт в памяти: после перезапуска ротация начинается с первой темы.
    Этого достаточно для канала; если понадобится «помнить» позицию между
    рестартами — замените индекс на чтение/запись маленького JSON-файла.
    """

    def __init__(self, topics: List[str]) -> None:
        self._topics = topics
        self._index = 0

    def next_topic(self) -> str:
        topic = self._topics[self._index % len(self._topics)]
        self._index += 1
        return topic


class _Context:
    """Глобальный контекст приложения. Заполняется один раз в main()."""

    settings: Settings
    ai: DeepSeekClient
    rotation: TopicRotation


ctx = _Context()

# Черновики, ожидающие нажатия кнопки «Опубликовать в канал»
_pending: Dict[str, GeneratedContent] = {}


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пускает дальше только владельца бота.

    Вешается как outer_middleware на message и callback_query, то есть
    срабатывает до фильтров: чужой не сможет ни сгенерировать пост, ни нажать
    кнопку публикации. Автопостинг по расписанию middleware не касается —
    ему и не нужно, он идёт изнутри процесса.
    """

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        owners = ctx.settings.owner_ids

        if user is not None and user.id in owners:
            return await handler(event, data)

        if user is None:
            logger.warning("Доступ запрещён: апдейт без пользователя")
            return None

        # В группах (в том числе в чате обсуждения канала) не отвечаем:
        # иначе после каждого поста бот пишет «это приватный бот» при реплаях.
        chat = getattr(event, "chat", None)
        if chat is None and isinstance(event, CallbackQuery) and event.message is not None:
            chat = event.message.chat
        if chat is not None and chat.type != "private":
            logger.info("Пропуск сообщения в чате %s (тип %s)", chat.id, chat.type)
            return None

        logger.warning("Доступ запрещён: id=%s, %s", user.id, f"@{user.username}" if user.username else "без username")

        if not owners:
            text = (
                "Это приватный бот. Владелец ещё не задал OWNER_ID.\n"
                f"Ваш Telegram id: {user.id} — передайте его владельцу."
            )
        else:
            text = "Это приватный бот. Генерация и публикация доступны только владельцу."

        if isinstance(event, CallbackQuery):
            await event.answer(text[:190], show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
        return None


# ---------------------------------------------------------------------------
# Генерация контента
# ---------------------------------------------------------------------------
async def generate_content(topic: str) -> GeneratedContent:
    """Полный цикл: генерация → проверка объёма → проверка → исправление → картинка."""
    settings, ai = ctx.settings, ctx.ai
    notes: List[str] = []

    raw = await ai.generate_post(topic)
    sections = parse_sections(raw)
    post = clean_post(sections["post"])
    if not post:
        raise AIError("DeepSeek не вернул блок [ПОСТ]")

    image_prompt = sections["image"].strip()
    model_check = sections["check"].strip()

    # --- Объём: одна автоматическая попытка уложиться в 250–400 символов ---
    length_note = check_length(post, settings.post_min_chars, settings.post_max_chars)
    if length_note:
        logger.info("Длина поста вне нормы (%s), прошу переписать", length_note)
        instruction = (
            f"Сократи пост до {settings.post_min_chars}–{settings.post_max_chars} символов,"
            " сохрани смысл, заголовок, призыв и хэштеги."
            if len(post) > settings.post_max_chars
            else f"Расширь пост до {settings.post_min_chars}–{settings.post_max_chars} символов,"
            " добавь полезную конкретику, сохрани призыв и хэштеги."
        )
        try:
            post = await ai.rewrite_post(post, instruction)
            notes.append("Объём подогнан под 250–400 символов.")
        except AIError as exc:
            logger.warning("Не удалось поправить объём: %s", exc)
            notes.append(f"Объём вне нормы ({length_note}).")

    # Страховка: модель не всегда слушается — режем сами, но по границе предложения
    if len(post) > settings.post_max_chars:
        fitted = _fit_length(post, settings.post_max_chars)
        if fitted != post:
            logger.warning("Пост обрезан до %s символов (было %s)", len(fitted), len(post))
            notes.append(f"Пост обрезан автоматически до {len(fitted)} символов.")
            post = fitted

    # --- Проверка и картинка идут параллельно: экономим время пользователя ---
    review_result, image_result = await asyncio.gather(
        ai.review_post(post),
        fetch_image(image_prompt, settings),
        return_exceptions=True,
    )

    review_ok = False
    review_text = model_check or "Модель не прислала блок проверки."
    if isinstance(review_result, AIError):
        logger.warning("Второй запрос (проверка) не удался: %s", review_result)
        notes.append("Автопроверка недоступна — просмотрите пост вручную.")
    elif isinstance(review_result, BaseException):
        logger.error("Неожиданная ошибка проверки: %s", review_result)
        notes.append("Автопроверка упала с ошибкой — нужна ручная проверка.")
    else:
        review_text, review_ok = review_result.raw, review_result.ok

    # --- Серьёзные замечания: одна попытка исправить и перепроверить ---
    if not review_ok:
        try:
            fixed = await ai.rewrite_post(
                post,
                "Исправь пост по замечаниям редактора и убери все проблемы:\n" + review_text,
            )
            second = await ai.review_post(fixed)
            post = fixed
            review_text = f"{review_text}\n\n— После исправления —\n{second.raw}"
            review_ok = second.ok
            notes.append("Пост переписан после замечаний проверки.")
        except AIError as exc:
            logger.warning("Автоисправление не удалось: %s", exc)
            notes.append("Исправить автоматически не вышло — нужна ручная проверка.")

    image: Optional[bytes] = None
    if isinstance(image_result, BaseException):
        logger.error("Неожиданная ошибка загрузки картинки: %s", image_result)
        notes.append("Картинку получить не удалось — пост без изображения.")
    elif image_result is None:
        notes.append("Картинку получить не удалось — пост без изображения.")
    else:
        image = image_result

    final_length = check_length(post, settings.post_min_chars, settings.post_max_chars)
    if final_length:
        notes.append(f"Итоговая длина: {len(post)} символов ({final_length}).")

    return GeneratedContent(
        topic=topic,
        post=post,
        image_prompt=image_prompt,
        review_text=review_text,
        review_ok=review_ok,
        image=image,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Отправка и публикация
# ---------------------------------------------------------------------------
def _fit_length(text: str, limit: int) -> str:
    """Аварийная обрезка: пост не должен уходить в канал длиннее лимита.

    Хэштеги сохраняем отдельным блоком, тело режем по границе предложения,
    а если предложение одно — по последнему пробелу.
    """
    if len(text) <= limit:
        return text

    hashtags = " ".join(re.findall(r"#[\wА-Яа-яЁё]+", text))
    body = re.sub(r"#[\wА-Яа-яЁё]+", "", text).strip()

    budget = limit - (len(hashtags) + 2 if hashtags else 0)
    if budget < 120:          # хэштеги съели слишком много места — жертвуем ими
        hashtags, budget = "", limit

    cut = body[:budget]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if boundary > budget * 0.6:
        cut = cut[: boundary + 1]
    elif " " in cut:
        cut = cut[: cut.rfind(" ")]
    body = cut.rstrip(" ,;:-—")

    return f"{body}\n\n{hashtags}".strip() if hashtags else body


def format_review(content: GeneratedContent) -> str:
    """Сообщение «Проверка перед публикацией» с пометкой статуса."""
    status = "✅ замечаний нет" if content.review_ok else "⚠️ есть замечания"
    parts = [
        f"🔎 <b>Проверка перед публикацией</b> — {status}",
        "",
        html.escape(content.review_text.strip()),
    ]
    if content.notes:
        parts.extend(["", "<i>" + html.escape(" ".join(content.notes)) + "</i>"])
    return "\n".join(parts)[:TEXT_LIMIT]

# Путь переопределяется в .env: на хостинге удобно держать файл на постоянном диске
DRAFTS_FILE = os.getenv("DRAFTS_FILE", "drafts.json")

def _serialize(content) -> dict:
    """Превращает GeneratedContent в JSON-совместимый словарь."""
    # dict(...) обязателен: иначе замена bytes -> {"__bytes__": ...} портит
    # живой объект, и send_photo получает словарь вместо картинки
    data = dict(content.__dict__) if hasattr(content, "__dict__") else dict(content)
    for key, value in list(data.items()):
        if isinstance(value, bytes):
            data[key] = {"__bytes__": base64.b64encode(value).decode("utf-8")}
    return data

def _deserialize(data: dict) -> GeneratedContent:
    """Собирает GeneratedContent обратно из словаря."""
    kwargs = dict(data)
    for key, value in list(kwargs.items()):
        if isinstance(value, dict) and "__bytes__" in value:
            kwargs[key] = base64.b64decode(value["__bytes__"])
    return GeneratedContent(**kwargs)

def _flush_pending() -> None:
    """Сохраняет все черновики на диск."""
    try:
        store = {t: _serialize(c) for t, c in _pending.items()}
        with open(DRAFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Не удалось сохранить черновики: {e}")

def _load_pending() -> None:
    """Восстанавливает черновики с диска при запуске бота."""
    global _pending
    if not os.path.exists(DRAFTS_FILE):
        # Это норма: до первого /post файла просто нет
        logger.info("Черновики: файла %s ещё нет, загружать нечего", DRAFTS_FILE)
        _pending = {}
        return

    try:
        with open(DRAFTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Черновики: не удалось прочитать %s (%s), стартую с пустым списком", DRAFTS_FILE, exc)
        _pending = {}
        return

    restored = {}
    for token, item in (raw.items() if isinstance(raw, dict) else []):
        try:
            content = _deserialize(item)
        except Exception as exc:  # битая запись не должна ронять старт бота
            logger.warning("Черновик %s пропущен: %s", token, exc)
            continue
        if content.post:
            restored[token] = content

    _pending = restored
    logger.info(f"Загружено черновиков из файла: {len(_pending)}")

def _remember(content: GeneratedContent) -> str:
    """Кладёт черновик в память и на диск, возвращает токен для callback."""
    token = uuid.uuid4().hex[:12]
    _pending[token] = content
    while len(_pending) > PENDING_LIMIT:
        _pending.pop(next(iter(_pending)))
    _flush_pending()
    return token



async def send_content(
    bot: Bot,
    chat_id: Union[int, str],
    content: GeneratedContent,
    *,
    offer_publish: bool = False,
) -> None:
    """Картинка с подписью-постом + отдельное сообщение с проверкой."""
    caption = html.escape(content.post)
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 1] + "…"

    markup: Optional[InlineKeyboardMarkup] = None
    if offer_publish and ctx.settings.channel_id:
        token = _remember(content)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Опубликовать в канал",
                        callback_data=f"pub:{token}",
                    )
                ]
            ]
        )

    if content.image:    
        image = content.image
        if isinstance(image, str):
            # если это data URI вида "data:image/jpeg;base64,...." — отрезаем заголовок
            if image.startswith("data:"):
                image = image.split(",", 1)[1]
            image = base64.b64decode(image)
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(_image_bytes(content.image), filename="post.jpg"),
            caption=caption,
        )
    else:
        await bot.send_message(chat_id, caption)

    await bot.send_message(chat_id, format_review(content), reply_markup=markup)

def _image_bytes(raw):
    """Приводит картинку к байтам: если пришла строка — декодирует."""
    if isinstance(raw, str):
        data = raw.split(",", 1)[1] if raw.startswith("data:") else raw
        return base64.b64decode(data)
    return raw

async def publish_to_channel(bot: Bot, chat_id: Union[int, str], content: GeneratedContent) -> None:
    """Публикует только пост с картинкой: проверка — служебная информация."""
    caption = html.escape(content.post)
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 1] + "…"
    if content.image:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(_image_bytes(content.image), filename="post.jpg"),
            caption=caption,
        )
    else:
        await bot.send_message(chat_id, caption)


# ---------------------------------------------------------------------------
# Автопостинг
# ---------------------------------------------------------------------------
async def autopost(bot: Bot) -> None:
    """Работа по расписанию: сгенерировать пост и опубликовать в канал."""
    settings = ctx.settings
    if not settings.channel_id:
        logger.warning("Автопостинг пропущен: CHANNEL_ID не задан")
        return

    topic = ctx.rotation.next_topic()
    try:
        content = await generate_content(topic)
    except AIError as exc:
        logger.error("Автопостинг: генерация не удалась — %s", exc)
        return
    except Exception:  # noqa: BLE001 — задача планировщика не должна падать
        logger.exception("Автопостинг: неожиданная ошибка генерации")
        return

    logger.info("Автопостинг: тема «%s», длина %s символов", topic, len(content.post))
    logger.info("Проверка перед публикацией:\n%s", content.review_text)

    if not content.review_ok:
        logger.warning("Автопостинг остановлен: пост не прошёл проверку")
        return

    try:
        await publish_to_channel(bot, settings.channel_id, content)
        logger.info("Автопостинг: пост опубликован в %s", settings.channel_id)
    except Exception:  # noqa: BLE001
        logger.exception("Автопостинг: не удалось отправить пост в канал %s", settings.channel_id)


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------
START_TEXT = (
    "<b>Привет! Я собираю посты для канала о спорте и питании.</b>\n\n"
    "Что умею:\n"
    "• /post — пост на очередную тему из ротации\n"
    "• /topic &lt;тема&gt; — пост на вашу тему\n"
    "• /help — краткая справка\n\n"
    "В каждом посте: заголовок, короткий текст, призыв к действию и хэштеги. "
    "К посту прилагается картинка и независимая проверка на ошибки и опасные советы."
)

HELP_TEXT = (
    "<b>Справка</b>\n\n"
    "/post — очередная тема из списка: тренировка, питание, восстановление, мифы, микрошаг.\n"
    "/topic &lt;тема&gt; — свой запрос, например: /topic тренировка дома 20 минут\n"
    "/help — эта справка.\n\n"
    "Пост приходит картинкой с подписью; следом — сообщение «Проверка перед публикацией». "
    "Кнопка под проверкой отправляет пост в канал из CHANNEL_ID."
)

NO_PUBLISH_HINT = (
    "\n\n<i>CHANNEL_ID не задан — кнопка публикации недоступна. "
    "Добавьте CHANNEL_ID в .env.</i>"
)

PUBLIC_START_TEXT = (
    "<b>Привет! Это сервис тренировок и питания.</b>\n\n"
    "Здесь будут планы тренировок под ваши параметры и цель, расчёт калорий "
    "и техника упражнений.\n\n"
    "• /sub — подписка и бесплатный пробный период\n"
    "• /help — справка\n\n"
    "Первую неделю можно попробовать бесплатно."
)

PUBLIC_HELP_TEXT = (
    "<b>Справка</b>\n\n"
    "/sub — подписка: пробный период, оплата, статус доступа\n"
    "/help — эта справка\n\n"
    "Доступ открывается на 30 дней после оплаты. "
    "Вопросы по платежам — /paysupport."
)


PAYWALL_TEXT = (
    "Это входит в подписку.\n\n"
    "Пробный период — 7 дней бесплатно, дальше 349 ₽ за 30 дней.\n"
    "Без подписки остаются калькулятор калорий и 17 базовых упражнений."
)


def _paywall_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Попробовать / оформить", callback_data="sub:menu")
        ]]
    )


async def _ensure_access(message: Message, user_id: int) -> bool:
    """False и сообщение-заглушка, если подписки нет."""
    if await subs.has_access(user_id):
        return True
    await message.answer(PAYWALL_TEXT, reply_markup=_paywall_markup())
    return False


def _is_owner(user_id: int) -> bool:
    return user_id in ctx.settings.owner_ids


# ---------------------------------------------------------------------------
# Кнопки меню
# ---------------------------------------------------------------------------
BTN_SUB = "💳 Подписка"
BTN_EX = "🏋️ Упражнения"
BTN_PLAN = "📋 Мой план"
BTN_CAL = "🧮 Калории"
BTN_DIARY = "🍽 Дневник"
BTN_APP = "📱 Приложение"
BTN_HELP = "ℹ️ Помощь"
BTN_POST = "📝 Новый пост"
BTN_TOPIC = "✍️ Пост на тему"
BTN_BACKUP = "💾 Бэкап"

MENU_TEXTS = {BTN_SUB, BTN_EX, BTN_PLAN, BTN_CAL, BTN_DIARY, BTN_APP, BTN_HELP, BTN_POST, BTN_TOPIC, BTN_BACKUP}


def _public_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUB), KeyboardButton(text=BTN_EX)],
            [KeyboardButton(text=BTN_PLAN), KeyboardButton(text=BTN_CAL)],
            [KeyboardButton(text=BTN_DIARY), KeyboardButton(text=BTN_APP)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _owner_menu() -> ReplyKeyboardMarkup:
    # Админские кнопки — в первых строках: Telegram обрезает длинные клавиатуры,
    # и раньше «Пост»/«Бэкап» уезжали вниз, где их не видно.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_POST), KeyboardButton(text=BTN_TOPIC)],
            [KeyboardButton(text=BTN_BACKUP), KeyboardButton(text=BTN_APP)],
            [KeyboardButton(text=BTN_SUB), KeyboardButton(text=BTN_EX)],
            [KeyboardButton(text=BTN_PLAN), KeyboardButton(text=BTN_CAL)],
            [KeyboardButton(text=BTN_DIARY), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _menu_for(user_id: int) -> ReplyKeyboardMarkup:
    return _owner_menu() if _is_owner(user_id) else _public_menu()


@pay_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Публичное приветствие; владельцу показываем админскую памятку."""
    if message.from_user and _is_owner(message.from_user.id):
        await message.answer(START_TEXT, reply_markup=_owner_menu())
        return
    await message.answer(PUBLIC_START_TEXT, reply_markup=_public_menu())


@pay_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if message.from_user and _is_owner(message.from_user.id):
        text = HELP_TEXT if ctx.settings.channel_id else HELP_TEXT + NO_PUBLISH_HINT
    else:
        text = PUBLIC_HELP_TEXT
    user_id = message.from_user.id if message.from_user else 0
    await message.answer(text, reply_markup=_menu_for(user_id))


@pay_router.callback_query(F.data == "sub:menu")
async def cb_sub_menu(callback: CallbackQuery) -> None:
    status = await subs.get_status(callback.from_user.id)
    text, markup = _sub_screen(status)
    if callback.message:
        try:
            await callback.message.answer(text, reply_markup=markup)
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось показать экран подписки")
    await callback.answer()


@router.message(Command("post"))
async def cmd_post(message: Message) -> None:
    await _generate_and_send(message, ctx.rotation.next_topic())


@router.message(Command("topic"))
async def cmd_topic(message: Message, command: CommandObject) -> None:
    topic = (command.args or "").strip()
    if not topic:
        await message.answer(
            "Укажите тему после команды, например:\n"
            "<code>/topic сон и восстановление после тренировок</code>"
        )
        return
    if len(topic) > 300:  # защита от простыни текста в запросе
        topic = topic[:300]
    await _generate_and_send(message, topic)


async def _generate_and_send(message: Message, topic: str) -> None:
    """Общая обёртка для /post и /topic: статус, генерация, отправка, ошибки."""
    status = await message.answer(f"⏳ Генерирую пост: <i>{html.escape(topic)}</i>")

    try:
        content = await generate_content(topic)
    except AIError as exc:
        logger.warning("Генерация не удалась: %s", exc)
        await _safe_edit(status, f"😔 Не получилось: {html.escape(str(exc))}\nПопробуйте ещё раз через минуту.")
        return
    except Exception:  # noqa: BLE001 — бот не должен падать из-за одного запроса
        logger.exception("Неожиданная ошибка генерации")
        await _safe_edit(status, "😔 Внутренняя ошибка, детали в логах.")
        return

    await _safe_delete(status)

    offer_publish = str(message.chat.id) != str(ctx.settings.channel_id)
    try:
        await send_content(message.bot, message.chat.id, content, offer_publish=offer_publish)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить пост в чат %s", message.chat.id)


@router.message(F.text == BTN_POST)
async def btn_post(message: Message) -> None:
    await cmd_post(message)


@router.message(F.text == BTN_TOPIC)
async def btn_topic(message: Message) -> None:
    await message.answer(
        "Напишите тему одной командой:\n"
        "<code>/topic сон и восстановление</code>\n"
        "Например: <code>/topic микротренировка 15 минут дома</code>"
    )


@router.message(F.text == BTN_BACKUP)
async def btn_backup(message: Message) -> None:
    await cmd_backup(message)


async def _safe_edit(message: Message, text: str) -> None:
    """edit_text может упасть (сообщение удалено, нет прав) — не роняем хендлер."""
    try:
        await message.edit_text(text)
    except Exception:  # noqa: BLE001
        logger.debug("Не удалось отредактировать служебное сообщение")


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        logger.debug("Не удалось удалить служебное сообщение")


@router.callback_query(F.data.startswith("pub:"))
async def on_publish(callback: CallbackQuery) -> None:
    """Публикация черновика в канал по кнопке."""
    token = (callback.data or "").split(":", 1)[-1]
    content = _pending.pop(token, None)

    if content is None:
        await callback.answer("Черновик устарел — сгенерируйте пост заново", show_alert=True)
        return
    if not ctx.settings.channel_id:
        await callback.answer("CHANNEL_ID не задан в .env", show_alert=True)
        return
    if not content.review_ok:
        await callback.answer("Проверка нашла замечания — публикую как есть", show_alert=True)

    try:
        await publish_to_channel(callback.bot, ctx.settings.channel_id, content)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось опубликовать пост в канал")
        await callback.answer("Не удалось опубликовать, детали в логах", show_alert=True)
        return

    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        logger.debug("Не удалось убрать кнопку публикации")

    await callback.answer("Опубликовано ✅")


@pay_router.message(F.text == BTN_SUB)
async def btn_sub(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_sub(message)


@pay_router.message(F.text == BTN_HELP)
async def btn_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_help(message)


# ---------------------------------------------------------------------------
# Каталог упражнений
# ---------------------------------------------------------------------------
def _groups_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for group in exercises.groups():
        row.append(InlineKeyboardButton(text=group, callback_data=f"ex:grp:{group}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


PAGE_SIZE = 6


async def _restricted_slugs(user_id: int) -> set:
    """Упражнения, которые не рекомендованы при ограничениях пользователя."""
    profile = await profiles.get(user_id)
    return planner.excluded_slugs(profile.limits) if profile else set()


def _group_keyboard(
    group: str, page: int = 0, restricted: set | None = None, unlocked: bool = False
) -> InlineKeyboardMarkup:
    items = exercises.by_group(group)
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE

    restricted = restricted or set()
    rows = [
        [
            InlineKeyboardButton(
                text=("⚠️ " if ex.slug in restricted else "")
                     + f"{ex.name_ru}{'' if (ex.free or unlocked) else ' 🔒'}",
                callback_data=f"ex:show:{ex.slug}",
            )
        ]
        for ex in items[start:start + PAGE_SIZE]
    ]

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="←", callback_data=f"ex:grp:{group}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ex:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="→", callback_data=f"ex:grp:{group}:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="← Группы", callback_data="ex:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_exercise(callback: CallbackQuery, ex: "exercises.Exercise", warning: str = "") -> None:
    caption = warning + ex.card_text()
    images = ex.images()
    if not images:
        await callback.message.answer(caption)
        await callback.answer()
        return

    sent = None
    cached_ids: Dict[int, str] = {}
    for attempt in (1, 2):
        cached = await exercises.cached_file_ids(ex.slug) if attempt == 1 else {}
        if attempt == 1:
            cached_ids = cached
        media = []
        for idx, path in enumerate(images):
            source = cached.get(idx) or FSInputFile(path)
            media.append(
                InputMediaPhoto(media=source, caption=caption if idx == 0 else None)
            )
        try:
            sent = await callback.message.answer_media_group(media)
            break
        except Exception:  # noqa: BLE001
            if attempt == 1 and cached:
                # file_id мог устареть (сменился токен, файл удалён) — грузим заново
                logger.warning("Кэш file_id не сработал для %s, загружаю файлы заново", ex.slug)
                await exercises.forget_file_ids(ex.slug)
                continue
            logger.exception("Не удалось отправить карточку %s", ex.slug)
            await callback.answer("Не получилось отправить карточку", show_alert=True)
            return

    if sent is None:
        await callback.answer("Не получилось отправить карточку", show_alert=True)
        return

    ids = {}
    for idx, message in enumerate(sent):
        if getattr(message, "photo", None):
            ids[idx] = message.photo[-1].file_id
    # Пишем в БД только если идентификаторы действительно новые
    if ids and ids != cached_ids:
        await exercises.remember_file_ids(ex.slug, ids)
    await callback.answer()


@pay_router.message(F.text == BTN_EX)
async def btn_exercises(message: Message, state: FSMContext) -> None:
    await state.clear()
    total = len(exercises.all_exercises())
    if total == 0:
        await message.answer("Каталог упражнений пока пуст.")
        return
    free = sum(1 for e in exercises.all_exercises() if e.free)
    await message.answer(
        f"<b>Упражнения</b>\n{total} карточек, {free} доступны бесплатно.\n"
        "Выберите группу мышц:",
        reply_markup=_groups_keyboard(),
    )


@pay_router.callback_query(F.data == "ex:back")
async def cb_ex_back(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(
                "Выберите группу мышц:", reply_markup=_groups_keyboard()
            )
        except Exception:  # noqa: BLE001 — «message is not modified» и прочее
            logger.debug("Не удалось обновить меню групп")
    await callback.answer()


@pay_router.callback_query(F.data == "ex:noop")
async def cb_ex_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@pay_router.callback_query(F.data.startswith("ex:grp:"))
async def cb_ex_group(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    group = parts[2] if len(parts) > 2 else ""
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    items = exercises.by_group(group)
    if not items:
        await callback.answer("В этой группе пока пусто", show_alert=True)
        return
    free = sum(1 for e in items if e.free)
    restricted = await _restricted_slugs(callback.from_user.id)
    unlocked = await subs.has_access(callback.from_user.id)
    note = "\n⚠️ — не рекомендуется при ваших ограничениях." if restricted else ""
    if callback.message:
        try:
            await callback.message.edit_text(
                f"<b>{group}</b> — упражнений: {len(items)}, бесплатных: {free}.{note}\n"
                "Нажмите карточку:",
                reply_markup=_group_keyboard(group, page, restricted, unlocked),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось обновить список упражнений группы %s", group)
    await callback.answer()


@pay_router.callback_query(F.data.startswith("ex:show:"))
async def cb_ex_show(callback: CallbackQuery) -> None:
    slug = (callback.data or "").split(":", 2)[2]
    ex = exercises.by_slug(slug)
    if ex is None:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return

    if not ex.free and not await subs.has_access(callback.from_user.id):
        await callback.answer("Это упражнение по подписке", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Эта карточка входит в подписку.\n"
                f"Пробный период — {subs.TRIAL_DAYS} дней бесплатно.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="Оформить подписку", callback_data="sub:menu")
                    ]]
                ),
            )
        return

    restricted = await _restricted_slugs(callback.from_user.id)
    warning = ""
    if ex.slug in restricted:
        warning = "⚠️ <i>С учётом ваших ограничений это упражнение не рекомендуется.</i>\n\n"
    await _send_exercise(callback, ex, warning)


# ---------------------------------------------------------------------------
# Анкета и план тренировок
# ---------------------------------------------------------------------------
class ProfileForm(StatesGroup):
    height = State()
    weight = State()


def _kb(options: dict, prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{prefix}{key}")]
            for key, label in options.items()
        ]
    )


DAYS_OPTIONS = {2: "2 дня в неделю", 3: "3 дня в неделю", 4: "4 дня в неделю", 5: "5 дней в неделю"}
MINUTES_OPTIONS = {20: "20 минут", 30: "30 минут", 45: "45 минут", 60: "60 минут"}


async def _plan_weights(user_id: int, profile: "profiles.Profile", plan, week=None) -> dict:
    """Рекомендованные веса по истории: подставляются прямо в план."""
    week = week if week is not None else await progress.week_state(user_id, len(plan.days))
    breakdown = await progress.breakdown(user_id)
    weights: dict = {}
    for day_index, day in enumerate(plan.days):
        allow = (
            not week["deload"]
            and not week["after_deload"]
            and day_index not in week.get("partial", [])
        )
        for slot in day.slots:
            slug = slot.exercise.slug
            if slug in weights:
                continue
            sets = await progress.last_sets(user_id, slug)
            if not sets:
                continue
            reasons = breakdown.get(slot.exercise.group, {})
            allow_group = allow and not reasons.get("pain")
            step = progress.next_plan(
                profile.goal, slot.exercise.equipment, sets, allow_increase=allow_group
            )
            weight = step["weight"]
            # две неудачи именно по весу — временно снижаем нагрузку
            if reasons.get("weight", 0) >= 2 and weight > 0:
                weight = max(0.0, weight - progress._step_for(slot.exercise.equipment))
            weights[slug] = {
                "label": planner.format_weight(weight, slot.exercise.equipment),
                "reps": None if step["bodyweight"] else step["reps"],
            }
    return weights


async def _show_plan(message: Message, profile: "profiles.Profile") -> None:
    plan = planner.build(profile)
    if not plan.days:
        await message.answer(plan.note)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Записать тренировку", callback_data="wk:pick"),
                InlineKeyboardButton(text="🟡 Частично", callback_data="wk:part"),
            ],
            [InlineKeyboardButton(text="😕 Не смог", callback_data="wk:skip")],
            [
                InlineKeyboardButton(text="📈 Прогресс", callback_data="wk:report"),
                InlineKeyboardButton(text="⏰ Напоминания", callback_data="rm:menu"),
            ],
            [
                InlineKeyboardButton(text="📅 История", callback_data="wk:history"),
                InlineKeyboardButton(text="📋 Детали", callback_data="wk:details"),
            ],
            [InlineKeyboardButton(text="✏️ Изменить анкету", callback_data="pf:edit")],
            [InlineKeyboardButton(text="🏋️ Упражнения", callback_data="ex:back")],
        ]
    )
    user_id = message.from_user.id if message.from_user else message.chat.id
    week = await progress.week_state(user_id, len(plan.days))
    weights = await _plan_weights(user_id, profile, plan, week)
    weak = {group for group, _count in await progress.weak_groups(user_id)}
    breakdown = await progress.breakdown(user_id)
    pain = {group for group, reasons in breakdown.items() if reasons.get("pain")}
    await message.answer(
        planner.render(profile, plan, weights, deload=week["deload"],
                       weak_groups=weak, pain_groups=pain),
        reply_markup=markup,
    )


async def _start_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Соберу план под вас. Шаг 1 — цель:",
        reply_markup=_kb(profiles.GOALS, "pf:goal:"),
    )


@pay_router.message(F.text == BTN_PLAN)
@pay_router.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext) -> None:
    await state.clear()  # незавершённая анкета не должна мешать
    if message.from_user is None:
        return

    # План — платная функция; анкета без подписки не запускается
    if not await _ensure_access(message, message.from_user.id):
        return

    profile = await profiles.get(message.from_user.id)
    if profile is None:
        await _start_profile(message, state)
        return
    await _show_plan(message, profile)


@pay_router.callback_query(F.data == "pf:edit")
async def pf_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await callback.message.answer(
            "Пересоберём план. Цель:",
            reply_markup=_kb(profiles.GOALS, "pf:goal:"),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("pf:goal:"))
async def pf_goal(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(goal=(callback.data or "").split(":")[2])
    if callback.message:
        await callback.message.edit_text(
            "Шаг 2 — опыт тренировок:",
            reply_markup=_kb(profiles.EXPERIENCE, "pf:exp:"),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("pf:exp:"))
async def pf_exp(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(experience=(callback.data or "").split(":")[2])
    if callback.message:
        await callback.message.edit_text(
            "Шаг 3 — сколько дней в неделю готовы тренироваться?",
            reply_markup=_kb(DAYS_OPTIONS, "pf:days:"),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("pf:days:"))
async def pf_days(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        days = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    if days not in DAYS_OPTIONS:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    await state.update_data(days=days)
    if callback.message:
        await callback.message.edit_text(
            "Шаг 4 — сколько минут на тренировку?",
            reply_markup=_kb(MINUTES_OPTIONS, "pf:min:"),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("pf:min:"))
async def pf_minutes(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        minutes = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    if minutes not in MINUTES_OPTIONS:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    await state.update_data(minutes=minutes)
    if callback.message:
        await callback.message.edit_text(
            "Шаг 5 — сколько вы двигаетесь вне тренировок?",
            reply_markup=_kb(profiles.ACTIVITY, "pf:act:"),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("pf:act:"))
async def pf_activity(callback: CallbackQuery, state: FSMContext) -> None:
    code = (callback.data or "").split(":")[2]
    if code not in profiles.ACTIVITY:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    await state.update_data(activity=code)
    if callback.message:
        await callback.message.edit_text(
            "Шаг 6 — что есть под рукой?",
            reply_markup=_kb(profiles.EQUIPMENT, "pf:eq:"),
        )
    await callback.answer()


def _limits_keyboard(selected: set) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if code in selected else "▫️ ") + title,
            callback_data=f"pf:lim:{code}",
        )]
        for code, title in profiles.LIMITATIONS.items()
    ]
    rows.append([InlineKeyboardButton(text="Готово →", callback_data="pf:lim:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@pay_router.callback_query(F.data.startswith("pf:eq:"))
async def pf_equipment(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(equipment=(callback.data or "").split(":")[2], limits=[])
    if callback.message:
        await callback.message.edit_text(
            "Шаг 7 — есть ли ограничения по здоровью?\n"
            "Отметьте всё, что беспокоит, или сразу нажмите «Готово».",
            reply_markup=_limits_keyboard(set()),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("pf:lim:"))
async def pf_limitations(callback: CallbackQuery, state: FSMContext) -> None:
    action = (callback.data or "").split(":")[2]
    data = await state.get_data()
    limits = set(data.get("limits") or [])

    if action == "done":
        await state.update_data(limits=sorted(limits))
        await state.set_state(ProfileForm.height)
        if callback.message:
            await callback.message.answer("Шаг 8 — ваш рост в сантиметрах? Например: 180")
        await callback.answer()
        return

    if action in profiles.LIMITATIONS:
        limits.symmetric_difference_update({action})
        await state.update_data(limits=sorted(limits))
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=_limits_keyboard(limits))
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось обновить клавиатуру ограничений")
        chosen = ", ".join(profiles.LIMITATIONS[c] for c in sorted(limits)) or "ничего"
        await callback.answer(f"Отмечено: {chosen}")
        return

    await callback.answer()


async def _abort_form(message: Message, state: FSMContext) -> bool:
    """Если во время анкеты нажали кнопку меню или команду — сбрасываем её."""
    text = (message.text or "").strip()
    if text.startswith("/") or text in MENU_TEXTS:
        await state.clear()
        await message.answer("Анкета сброшена. Начните заново: «📋 Мой план».")
        return True
    return False


@pay_router.message(StateFilter(ProfileForm.height), F.text)
async def pf_height(message: Message, state: FSMContext) -> None:
    if await _abort_form(message, state):
        return
    digits = re.sub(r"\D", "", message.text or "")
    height = int(digits) if digits else 0
    if not 100 <= height <= 250:
        await message.answer("Рост числом в сантиметрах, от 100 до 250. Например: 180")
        return
    await state.update_data(height=height)
    await state.set_state(ProfileForm.weight)
    await message.answer("Шаг 9 — вес в килограммах? Например: 78")


@pay_router.message(StateFilter(ProfileForm.weight), F.text)
async def pf_weight(message: Message, state: FSMContext) -> None:
    if await _abort_form(message, state):
        return
    digits = re.sub(r"\D", "", message.text or "")
    weight = int(digits) if digits else 0
    if not 30 <= weight <= 250:
        await message.answer("Вес числом в килограммах, от 30 до 250. Например: 78")
        return

    data = await state.get_data()
    await state.clear()

    if message.from_user is None:
        return

    # пол и возраст могли уже быть заданы в калькуляторе — не теряем их
    existing = await profiles.get(message.from_user.id)
    data["weight"] = weight
    profile = _make_profile(message.from_user.id, data, existing)
    await profiles.save(profile)
    await _show_plan(message, profile)


# ---------------------------------------------------------------------------
# Калории и БЖУ
# ---------------------------------------------------------------------------
AGE_OPTIONS = {
    22: "18–25 лет",
    30: "26–35 лет",
    40: "36–45 лет",
    50: "46–50 лет",
}


def _make_profile(user_id: int, data: dict, existing=None) -> "profiles.Profile":
    """Собирает профиль из ответов анкеты, сохраняя пол и возраст, если они были."""
    return profiles.Profile(
        user_id=user_id,
        height=int(data.get("height", 0)),
        weight=int(data.get("weight", 0)),
        experience=str(data.get("experience", "novice")),
        days=int(data.get("days", 3)),
        minutes=int(data.get("minutes", 45)),
        goal=str(data.get("goal", "tone")),
        equipment=str(data.get("equipment", "gym")),
        limitations=",".join(data.get("limits") or []),
        sex=existing.sex if existing else "",
        age=existing.age if existing else 0,
        activity=str(data.get("activity", existing.activity if existing else "")),
    )


async def _show_calories(message: Message, profile: "profiles.Profile") -> None:
    result = calories.compute(profile)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Изменить пол и возраст", callback_data="cal:edit")
        ]]
    )
    await message.answer(calories.render(profile, result), reply_markup=markup)


@pay_router.message(F.text == BTN_CAL)
@pay_router.message(Command("calories"))
async def cmd_calories(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    profile = await profiles.get(message.from_user.id)
    if profile is None:
        await message.answer(
            "Сначала соберите анкету — «📋 Мой план». "
            "Из неё берутся рост, вес, цель и активность."
        )
        return
    if not profile.has_body_data:
        await message.answer(
            "Для расчёта нужны пол и возраст.\nШаг 1 — пол:",
            reply_markup=_kb({"m": "Мужской", "f": "Женский"}, "cal:sex:"),
        )
        return
    if not profile.activity:
        await message.answer(
            "Сколько вы двигаетесь вне тренировок? Это заметно меняет расход калорий.",
            reply_markup=_kb(profiles.ACTIVITY, "cal:act:"),
        )
        return
    await _show_calories(message, profile)


@pay_router.callback_query(F.data.startswith("cal:act:"))
async def cal_activity(callback: CallbackQuery, state: FSMContext) -> None:
    code = (callback.data or "").split(":")[2]
    if code not in profiles.ACTIVITY:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    if callback.from_user is None:
        return
    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Анкета не найдена", show_alert=True)
        return
    profile = replace(profile, activity=code)
    await profiles.save(profile)
    await state.clear()
    if callback.message:
        await _show_calories(callback.message, profile)
    await callback.answer()


@pay_router.callback_query(F.data == "cal:edit")
async def cal_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await callback.message.answer(
            "Изменим пол и возраст.\nШаг 1 — пол:",
            reply_markup=_kb({"m": "Мужской", "f": "Женский"}, "cal:sex:"),
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("cal:sex:"))
async def cal_sex(callback: CallbackQuery, state: FSMContext) -> None:
    sex = (callback.data or "").split(":")[2]
    if sex not in ("m", "f"):
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    await state.update_data(cal_sex=sex)
    if callback.message:
        await callback.message.edit_text(
            "Шаг 2 — возраст:", reply_markup=_kb(AGE_OPTIONS, "cal:age:")
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("cal:age:"))
async def cal_age(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        age = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    if age not in AGE_OPTIONS:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    data = await state.get_data()
    sex = data.get("cal_sex")
    if sex not in ("m", "f") or callback.from_user is None:
        await callback.answer("Начните заново: «🧮 Калории»", show_alert=True)
        return

    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Анкета не найдена", show_alert=True)
        return

    profile = replace(profile, sex=sex, age=age)
    await profiles.save(profile)
    await state.clear()

    if callback.message:
        await _show_calories(callback.message, profile)
    await callback.answer()


# ---------------------------------------------------------------------------
# Дневник питания
# ---------------------------------------------------------------------------
class DiaryForm(StatesGroup):
    waiting = State()


MEAL_RE = re.compile(r"^(?P<name>.*?)[\s,]*?(?P<grams>\d{1,4})\s*(?:г|гр|грамм)?\.?$", re.IGNORECASE)


def _diary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="dr:add")],
            [
                InlineKeyboardButton(text="↩️ Удалить последнее", callback_data="dr:undo"),
                InlineKeyboardButton(text="🧹 Очистить", callback_data="dr:clear"),
            ],
        ]
    )


async def _diary_text(user_id: int) -> str:
    rows = await diary.items(user_id)
    totals = diary.totals(rows)
    profile = await profiles.get(user_id)

    target = None
    if profile is not None and profile.has_body_data and profile.activity:
        target = calories.compute(profile).target

    day = diary.today()
    lines = [
        f"<b>Дневник · {day[8:10]}.{day[5:7]}</b>",
        f"Съедено: <b>{totals['kcal']:.0f}</b> ккал · Б {totals['protein']:.0f} · "
        f"Ж {totals['fat']:.0f} · У {totals['carbs']:.0f}",
    ]
    if target:
        left = target - totals["kcal"]
        if left >= 0:
            lines.append(f"Норма: {target} ккал · осталось {left:.0f}")
        else:
            lines.append(f"Норма: {target} ккал · перебор {abs(left):.0f}")
    else:
        lines.append("Норма не рассчитана: заполните анкету и откройте «🧮 Калории».")

    lines.append("")
    if rows:
        start = max(0, len(rows) - 10)
        if start:
            lines.append(f"Показаны последние 10 из {len(rows)}:")
        for number, row in enumerate(rows[start:], start=start + 1):
            lines.append(f"{number}. {row['name']} {row['grams']:.0f} г — {row['kcal']:.0f} ккал")
    else:
        lines.append("Пока пусто. Нажмите «➕ Добавить».")
    return "\n".join(lines)


async def _show_diary(message: Message, user_id: int) -> None:
    await message.answer(await _diary_text(user_id), reply_markup=_diary_keyboard())


@pay_router.message(F.text == BTN_DIARY)
@pay_router.message(Command("diary"))
async def cmd_diary(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await _show_diary(message, message.from_user.id)


@pay_router.callback_query(F.data == "dr:add")
async def dr_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Дневник доступен по подписке", show_alert=True)
        return
    await state.set_state(DiaryForm.waiting)
    if callback.message:
        await callback.message.answer(
            "Что съели? Напишите продукт и граммы, можно несколько через запятую:\n"
            "<code>курица 150, рис 200, яблоко 120</code>"
        )
    await callback.answer()


@pay_router.callback_query(F.data == "dr:undo")
async def dr_undo(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Дневник доступен по подписке", show_alert=True)
        return
    removed = await diary.delete_last(callback.from_user.id)
    if callback.message:
        try:
            await callback.message.edit_text(
                await _diary_text(callback.from_user.id), reply_markup=_diary_keyboard()
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось обновить дневник")
    await callback.answer("Удалил последнюю запись" if removed else "Записей нет")


@pay_router.callback_query(F.data == "dr:clear")
async def dr_clear(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Дневник доступен по подписке", show_alert=True)
        return
    removed = await diary.clear(callback.from_user.id)
    if callback.message:
        try:
            await callback.message.edit_text(
                await _diary_text(callback.from_user.id), reply_markup=_diary_keyboard()
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось обновить дневник")
    await callback.answer(f"Удалено записей: {removed}")


@pay_router.message(StateFilter(DiaryForm.waiting), F.text)
async def diary_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/") or text in MENU_TEXTS:
        await state.clear()
        await message.answer("Ввод отменён. Откройте дневник заново.")
        return
    if message.from_user is None:
        return
    if not await subs.has_access(message.from_user.id):
        await state.clear()
        await message.answer(PAYWALL_TEXT, reply_markup=_paywall_markup())
        return

    parsed, errors = [], []
    for chunk in re.split(r"[,;\n]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = MEAL_RE.match(chunk)
        if not match:
            errors.append(f"«{chunk}» — не понял. Формат: курица 150")
            continue
        grams = int(match.group("grams"))
        if not 5 <= grams <= 2000:
            errors.append(f"«{chunk}» — граммы от 5 до 2000")
            continue
        food = diary.find_food(match.group("name"))
        if food is None:
            hints = diary.suggest(match.group("name"))
            hint = f" Похожие: {', '.join(hints)}." if hints else ""
            errors.append(f"«{match.group('name').strip()}» не нашёл.{hint}")
            continue
        parsed.append((food, grams))

    if errors:
        await message.answer(
            "\n".join(errors) + "\n\nПопробуйте ещё раз, например: <code>курица 150, рис 200</code>"
        )
        return

    for food, grams in parsed:
        await diary.add(message.from_user.id, food, grams)
    await state.clear()
    await _show_diary(message, message.from_user.id)


@pay_router.message(F.text == BTN_APP)
@pay_router.message(Command("app"))
async def cmd_app(message: Message, state: FSMContext) -> None:
    await state.clear()
    url = ctx.settings.webapp_url
    if not url:
        await message.answer(
            "Мини-апп ещё не подключён: задайте <code>WEBAPP_URL</code> в панели "
            "и сделайте Redeploy."
        )
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=url))
        ]]
    )
    await message.answer(
        "В приложении: план тренировок, упражнения с техникой, дневник питания и калории.",
        reply_markup=markup,
    )


# ---------------------------------------------------------------------------
# Прогресс: запись тренировок и отчёт
# ---------------------------------------------------------------------------
class WorkoutForm(StatesGroup):
    input = State()


class PartialForm(StatesGroup):
    exercises = State()
    reason = State()


PARTIAL_REASONS = {
    "reps": "не хватило повторений",
    "weight": "не осилил вес",
    "pain": "дискомфорт или боль",
}


def _partial_keyboard(day, selected: set) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if slot.exercise.slug in selected else "") + slot.exercise.name_ru,
            callback_data=f"part:t:{slot.exercise.slug}",
        )]
        for slot in day.slots
    ]
    rows.append([InlineKeyboardButton(text="Готово →", callback_data="part:next")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _progress_report(user_id: int) -> str:
    rows = await progress.history(user_id, 28)
    total = await progress.count(user_id)
    weak = await progress.weak_groups(user_id)
    if not rows:
        text = ("<b>Прогресс</b>\n\nПока пусто. Запишите первую тренировку — "
                "и здесь появится динамика рабочих весов.")
        if weak:
            text += "\n\nОтстают: " + ", ".join(f"{group} ({count})" for group, count in weak[:3])
        return text

    grouped: Dict[str, List] = {}
    for row in rows:
        grouped.setdefault(row["slug"], []).append(row)

    week = await progress.week_state(user_id, 0)
    header = f"<b>Прогресс</b>\nТренировок записано: {total}"
    if weak:
        header += "\nОтстают: " + ", ".join(f"{group} ({count})" for group, count in weak[:3])
    if week["done"]:
        header += f"\nНа этой неделе отмечено: {week['done']}"
    lines = [header + "\n"]
    items = []
    for slug, history in grouped.items():
        ex = exercises.by_slug(slug)
        if ex is None:
            continue
        first = float(history[0]["weight"])
        last = float(history[-1]["weight"])
        items.append((last - first, ex.name_ru, first, last, history[-1]["done_on"]))

    items.sort(reverse=True)
    for delta, name, first, last, day in items[:8]:
        if first <= 0:
            lines.append(f"• {name}: свой вес, последний раз {day[8:10]}.{day[5:7]}")
        elif abs(delta) < 0.01:
            lines.append(f"• {name}: {last:g} кг (без изменений)")
        else:
            sign = "+" if delta > 0 else "−"
            lines.append(f"• {name}: {first:g} → {last:g} кг ({sign}{abs(delta):g})")
    return "\n".join(lines)


@pay_router.callback_query(F.data == "wk:report")
async def wk_report(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Прогресс доступен по подписке", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(await _progress_report(callback.from_user.id))
    await callback.answer()


@pay_router.message(Command("progress"))
async def cmd_progress(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await message.answer(await _progress_report(message.from_user.id))


async def _history_text(user_id: int) -> str:
    data = await progress.history_weeks(user_id, 8)
    weeks = data["weeks"]
    if not weeks:
        return ("<b>История</b>\n\nПока пусто: запишите первую тренировку, "
                "и здесь появится стаж по неделям.")

    lines = [
        "<b>История</b>",
        f"Стаж: {data['experience']} нед. · тренировок: {data['total']}",
    ]
    if data["streak"]:
        lines.append(f"Серия: {data['streak']} нед. подряд")

    breakdown = await progress.breakdown(user_id)
    if breakdown:
        names = {"reps": "не хватило повторений", "weight": "не осилил вес",
                 "pain": "дискомфорт", "unknown": "без уточнения"}
        lines.append("")
        for group, reasons in list(breakdown.items())[:3]:
            parts = ", ".join(
                f"{names.get(reason, reason)} ×{count}" for reason, count in reasons.items()
            )
            lines.append(f"{group}: {parts}")

    lines.append("")

    for item in weeks:
        marks = []
        if item["done"]:
            marks.append(f"✅ {item['done']}")
        if item["partial"]:
            marks.append(f"🟡 {item['partial']}")
        if item["skipped"]:
            marks.append(f"😕 {item['skipped']}")
        if not marks:
            marks.append("— пусто")
        suffix = " · разгрузка" if item["deload"] and item["week"] % 4 == 0 else ""
        lines.append(f"Неделя {item['week']}: " + " · ".join(marks) + suffix)
    return "\n".join(lines)


DETAILS_PAGE = 10


def _details_markup(next_offset: int, has_more: bool):
    if not has_more:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Показать ещё", callback_data=f"wk:details:{next_offset}")
    ]])


async def _details_text(user_id: int, offset: int = 0):
    items = await progress.day_details(user_id, DETAILS_PAGE + 1, offset)
    has_more = len(items) > DETAILS_PAGE
    items = items[:DETAILS_PAGE]
    if not items:
        return ("<b>Детали по дням</b>\n\nБольше записей нет.", False)

    status = {"done": "✅ сделано", "partial": "🟡 частично", "skip": "😕 пропуск"}
    reason_names = {"reps": "не хватило повторений", "weight": "не осилил вес",
                    "pain": "дискомфорт", "unknown": "без уточнения"}

    lines = ["<b>Детали по дням</b>", ""]
    for item in items:
        day = item["date"]
        label = f"{day[8:10]}.{day[5:7]} · День {item['day_index'] + 1} · {status.get(item['status'], '—')}"
        if item["reasons"]:
            parts = []
            for group, reasons in list(item["reasons"].items())[:2]:
                inner = ", ".join(f"{reason_names.get(r, r)} ×{c}" for r, c in reasons.items())
                parts.append(f"{group}: {inner}")
            label += " · " + "; ".join(parts)
        lines.append(label)
    return "\n".join(lines), has_more


@pay_router.callback_query(F.data == "wk:details")
async def wk_details(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return
    text, has_more = await _details_text(callback.from_user.id, 0)
    if callback.message:
        await callback.message.answer(
            text, reply_markup=_details_markup(DETAILS_PAGE, has_more)
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("wk:details:"))
async def wk_details_more(callback: CallbackQuery) -> None:
    try:
        offset = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректная страница", show_alert=True)
        return
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return

    text, has_more = await _details_text(callback.from_user.id, offset)
    if callback.message:
        try:
            await callback.message.edit_text(
                text, reply_markup=_details_markup(offset + DETAILS_PAGE, has_more)
            )
        except Exception:  # noqa: BLE001
            await callback.message.answer(
                text, reply_markup=_details_markup(offset + DETAILS_PAGE, has_more)
            )
    await callback.answer()


@pay_router.callback_query(F.data == "wk:history")
async def wk_history(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(await _history_text(callback.from_user.id))
    await callback.answer()


@pay_router.message(Command("history"))
async def cmd_history(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await message.answer(await _history_text(message.from_user.id))


@pay_router.callback_query(F.data == "wk:pick")
async def wk_pick(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Запись тренировок доступна по подписке", show_alert=True)
        return
    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план: 📋 Мой план", show_alert=True)
        return

    plan = planner.build(profile)
    rows = [
        [InlineKeyboardButton(text=f"День {i + 1} · {day.title}", callback_data=f"wk:day:{i}")]
        for i, day in enumerate(plan.days)
    ]
    if callback.message:
        await callback.message.answer(
            "Какой день записываем?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    await callback.answer()


@pay_router.callback_query(F.data == "wk:skip")
async def wk_skip(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return
    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return

    plan = planner.build(profile)
    rows = [
        [InlineKeyboardButton(text=f"День {i + 1} · {day.title}", callback_data=f"wk:none:{i}")]
        for i, day in enumerate(plan.days)
    ]
    if callback.message:
        await callback.message.answer(
            "Какой день пропустили?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("wk:none:"))
async def wk_skip_day(callback: CallbackQuery) -> None:
    try:
        index = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный день", show_alert=True)
        return
    if callback.from_user is None:
        return

    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return

    plan = planner.build(profile)
    if index < 0 or index >= len(plan.days):
        await callback.answer("Такого дня нет", show_alert=True)
        return

    week = await progress.week_index(callback.from_user.id)
    await progress.set_check(callback.from_user.id, week, index, False)
    state = await progress.week_state(callback.from_user.id, len(plan.days))

    text = f"Отметил: день {index + 1} пропущен."
    if state["deload"]:
        text += "\nЭта неделя будет разгрузочной — нагрузка снизится, вес останется."
    if callback.message:
        await callback.message.answer(text)
    await callback.answer()


@pay_router.callback_query(F.data == "wk:part")
async def wk_part(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return
    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return

    plan = planner.build(profile)
    rows = [
        [InlineKeyboardButton(text=f"День {i + 1} · {day.title}", callback_data=f"wk:part:{i}")]
        for i, day in enumerate(plan.days)
    ]
    if callback.message:
        await callback.message.answer(
            "Какой день выполнили частично?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("wk:part:"))
async def wk_part_day(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный день", show_alert=True)
        return
    if callback.from_user is None or callback.message is None:
        return

    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return
    plan = planner.build(profile)
    if index < 0 or index >= len(plan.days):
        await callback.answer("Такого дня нет", show_alert=True)
        return

    day = plan.days[index]
    await state.set_state(PartialForm.exercises)
    await state.update_data(part_day=index, part_selected=[])
    await callback.message.answer(
        f"День {index + 1}: {day.title}\n\n"
        "Отметьте упражнения, которые не получились или дались тяжелее обычного.",
        reply_markup=_partial_keyboard(day, set()),
    )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("part:t:"))
async def part_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    slug = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    selected = set(data.get("part_selected") or [])
    index = int(data.get("part_day", 0))

    if slug in selected:
        selected.discard(slug)
    else:
        selected.add(slug)
    await state.update_data(part_selected=sorted(selected))

    profile = await profiles.get(callback.from_user.id) if callback.from_user else None
    if profile is None or callback.message is None:
        await callback.answer("Анкета не найдена", show_alert=True)
        return
    plan = planner.build(profile)
    if index < len(plan.days):
        try:
            await callback.message.edit_reply_markup(
                reply_markup=_partial_keyboard(plan.days[index], selected)
            )
        except Exception:  # noqa: BLE001
            pass
    await callback.answer()


@pay_router.callback_query(F.data == "part:next")
async def part_next(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("part_selected") or []
    if not selected:
        await callback.answer("Отметьте хотя бы одно упражнение", show_alert=True)
        return

    await state.set_state(PartialForm.reason)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"part:r:{code}")]
        for code, text in PARTIAL_REASONS.items()
    ])
    if callback.message:
        if len(selected) >= 3:
            hint = (
                f"Выбрано упражнений: {len(selected)}. Если причины разные, "
                "удобнее отметить по частям: сначала те, что не пошли по весу, "
                "потом отдельно те, где был дискомфорт. Каждая отметка суммируется."
            )
        else:
            hint = (
                f"Выбрано упражнений: {len(selected)} — причина применится ко всем. "
                "Если у разных упражнений причины разные, отметьте их отдельно: "
                "каждая отметка суммируется, и расчёт отстающих групп будет точнее."
            )
        await callback.message.answer(
            "Что именно не получилось?\n\n" + hint, reply_markup=markup
        )
    await callback.answer()


@pay_router.callback_query(F.data.startswith("part:r:"))
async def part_reason(callback: CallbackQuery, state: FSMContext) -> None:
    reason = (callback.data or "").split(":", 2)[2]
    if reason not in PARTIAL_REASONS:
        await callback.answer("Неизвестная причина", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get("part_selected") or []
    index = int(data.get("part_day", 0))
    if not selected or callback.from_user is None:
        await state.clear()
        await callback.answer("Начните заново", show_alert=True)
        return

    week = await progress.week_index(callback.from_user.id)
    await progress.set_check(callback.from_user.id, week, index, 2)
    await progress.add_failures(
        callback.from_user.id, week, index, [(slug, reason) for slug in selected]
    )
    await state.clear()

    weak = await progress.weak_groups(callback.from_user.id)
    lines = [
        f"Отметил: день {index + 1} выполнен частично.",
        f"Причина: {PARTIAL_REASONS[reason]}.",
        "Вес на следующую тренировку не повышаю.",
    ]
    if weak:
        lines.append("")
        lines.append("Чаще всего не получается: "
                     + ", ".join(f"{group} ({count})" for group, count in weak[:3]))
    lines.append("")
    lines.append("<i>Если у других упражнений причина иная — отметьте их ещё раз, "
                 "цифры сложатся.</i>")
    if callback.message:
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@pay_router.callback_query(F.data.startswith("wk:done:"))
async def wk_done(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный день", show_alert=True)
        return
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return

    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return
    plan = planner.build(profile)
    if index < 0 or index >= len(plan.days):
        await callback.answer("Такого дня нет", show_alert=True)
        return

    day = plan.days[index]
    await progress.add_workout(callback.from_user.id, index, day.title, [])
    await state.clear()

    if callback.message:
        await callback.message.answer(
            f"Записал: день {index + 1} · {day.title} выполнен полностью.\n\n"
            "Чтобы бот подсказывал вес на следующую тренировку, один раз впишите, "
            "с какими весами работали — кнопка «✍️ Детали» в плане. "
            "Для гантелей — вес одной гантели.\n"
            "Если что-то не получилось — отметьте «🟡 Частично», вес тогда не повысится."
        )
    await callback.answer("Записано")


@pay_router.callback_query(F.data.startswith("wk:day:"))
async def wk_day(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный день", show_alert=True)
        return
    if callback.from_user is None:
        return

    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return
    plan = planner.build(profile)
    if index < 0 or index >= len(plan.days):
        await callback.answer("Такого дня нет", show_alert=True)
        return

    day = plan.days[index]
    await state.set_state(WorkoutForm.input)
    await state.update_data(day_index=index)

    names = "\n".join(f"• {slot.exercise.name_ru}" for slot in day.slots)
    if callback.message:
        await callback.message.answer(
            f"<b>День {index + 1}: {day.title}</b>\n\n"
            "Если сделали всё по плану — нажмите «Всё выполнил». "
            "Вписывать упражнения нужно только когда что-то не получилось.\n\n"
            "Если вводите детали — по строке на упражнение:\n"
            "<code>приседания 60x8, 60x8, 60x7</code>\n\n"
            f"{names}\n\n"
            "Вес можно не указывать для упражнений с весом тела: <code>отжимания 0x12, 0x10</code>\n"
            "Для гантелей указывайте вес одной гантели: <code>жим гантелей 10x12</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Всё выполнил", callback_data=f"wk:done:{index}")
            ]]),
        )
    await callback.answer()


@pay_router.message(StateFilter(WorkoutForm.input), F.text)
async def wk_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/") or text in MENU_TEXTS:
        await state.clear()
        await message.answer("Запись отменена.")
        return
    if message.from_user is None:
        return

    data = await state.get_data()
    index = int(data.get("day_index", 0))
    profile = await profiles.get(message.from_user.id)
    if profile is None:
        await state.clear()
        await message.answer("Анкета не найдена — соберите план заново.")
        return

    plan = planner.build(profile)
    if index >= len(plan.days):
        await state.clear()
        await message.answer("План изменился. Откройте «📋 Мой план» и попробуйте снова.")
        return
    day = plan.days[index]
    allowed = {slot.exercise.slug: slot.exercise for slot in day.slots}

    records: List[progress.SetRecord] = []
    errors: List[str] = []
    touched: List[str] = []

    for line in text.splitlines():
        parsed = progress.parse_line(line)
        if parsed is None:
            if line.strip():
                errors.append(f"«{line.strip()[:30]}» — формат: упражнение 60x8, 60x8")
            continue
        name, sets = parsed
        ex = exercises.find_by_name_among(name, allowed)
        if ex is None:
            errors.append(f"«{name}» не нашёл в справочнике")
            continue
        if ex.slug not in allowed:
            errors.append(f"«{ex.name_ru}» не входит в этот день")
            continue
        if ex.slug not in touched:
            touched.append(ex.slug)
        for weight, reps in sets:
            records.append(progress.SetRecord(ex.slug, weight, reps))

    if not records:
        await message.answer(
            "\n".join(errors) + "\n\nПопробуйте ещё раз, например: <code>приседания 60x8, 60x8</code>"
        )
        return

    await progress.add_workout(message.from_user.id, index, day.title, records)
    week = await progress.week_index(message.from_user.id)
    await progress.set_check(message.from_user.id, week, index, True)
    await state.clear()

    week_state = await progress.week_state(message.from_user.id, len(plan.days))
    allow_increase = not week_state["deload"] and not week_state["after_deload"]

    lines = [f"<b>Записано: день {index + 1} · {day.title}</b>", ""]
    for slug in touched:
        ex = exercises.by_slug(slug)
        sets = await progress.last_sets(message.from_user.id, slug)
        advice = progress.next_advice(
            profile.goal, ex.equipment if ex else "", sets, allow_increase=allow_increase
        )
        lines.append(f"• {ex.name_ru if ex else slug} → {advice}")
    if errors:
        lines.append("")
        lines.append("<i>Пропущено: " + "; ".join(errors[:3]) + "</i>")

    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Напоминания о тренировках
# ---------------------------------------------------------------------------
WEEKDAY_NAMES = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
TIME_OPTIONS = ["07:00", "09:00", "12:00", "18:00", "19:00", "21:00"]


def _remind_text(profile: "profiles.Profile") -> str:
    if not profile.has_reminders:
        return ("<b>Напоминания</b>\n\nПока выключены. Выберите дни и время — "
                "бот напишет в эти дни с планом тренировки.")
    days = ", ".join(WEEKDAY_NAMES.get(d, str(d)) for d in profile.weekdays)
    return f"<b>Напоминания</b>\n\nСейчас: {days} в {profile.workout_time}"


def _remind_markup(profile: "profiles.Profile") -> InlineKeyboardMarkup:
    selected = set(profile.weekdays)
    day_buttons = [
        InlineKeyboardButton(
            text=("✅" if d in selected else "") + WEEKDAY_NAMES[d],
            callback_data=f"rm:day:{d}",
        )
        for d in WEEKDAY_NAMES
    ]
    rows = [day_buttons[:4], day_buttons[4:]]
    rows.append([
        InlineKeyboardButton(
            text=("• " if t == profile.workout_time else "") + t,
            callback_data=f"rm:time:{t}",
        )
        for t in TIME_OPTIONS[:3]
    ])
    rows.append([
        InlineKeyboardButton(
            text=("• " if t == profile.workout_time else "") + t,
            callback_data=f"rm:time:{t}",
        )
        for t in TIME_OPTIONS[3:]
    ])
    rows.append([InlineKeyboardButton(text="🔕 Выключить", callback_data="rm:off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_reminders(message: Message, user_id: int) -> None:
    profile = await profiles.get(user_id)
    if profile is None:
        await message.answer("Сначала соберите план: 📋 Мой план")
        return
    await message.answer(_remind_text(profile), reply_markup=_remind_markup(profile))


@pay_router.message(Command("remind"))
async def cmd_remind(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await _show_reminders(message, message.from_user.id)


@pay_router.callback_query(F.data == "rm:menu")
async def rm_menu(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    if not await subs.has_access(callback.from_user.id):
        await callback.answer("Доступно по подписке", show_alert=True)
        return
    profile = await profiles.get(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=_remind_markup(profile))
        except Exception:  # noqa: BLE001
            pass
        await callback.message.answer(_remind_text(profile), reply_markup=_remind_markup(profile))
    await callback.answer()


@pay_router.callback_query(F.data.startswith("rm:day:"))
async def rm_day(callback: CallbackQuery) -> None:
    try:
        day = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный день", show_alert=True)
        return
    profile = await profiles.get(callback.from_user.id) if callback.from_user else None
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return

    days = set(profile.weekdays)
    if day in days:
        days.discard(day)
    else:
        days.add(day)
    profile = replace(profile, workout_days=",".join(str(d) for d in sorted(days)))
    await profiles.save(profile)

    if callback.message:
        try:
            await callback.message.edit_text(_remind_text(profile), reply_markup=_remind_markup(profile))
        except Exception:  # noqa: BLE001
            pass
    await callback.answer()


@pay_router.callback_query(F.data.startswith("rm:time:"))
async def rm_time(callback: CallbackQuery) -> None:
    value = (callback.data or "").split(":")[2]
    profile = await profiles.get(callback.from_user.id) if callback.from_user else None
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return
    if value not in TIME_OPTIONS:
        await callback.answer("Некорректное время", show_alert=True)
        return

    profile = replace(profile, workout_time=value)
    await profiles.save(profile)
    if callback.message:
        try:
            await callback.message.edit_text(_remind_text(profile), reply_markup=_remind_markup(profile))
        except Exception:  # noqa: BLE001
            pass
    await callback.answer(f"Время: {value}")


@pay_router.callback_query(F.data == "rm:off")
async def rm_off(callback: CallbackQuery) -> None:
    profile = await profiles.get(callback.from_user.id) if callback.from_user else None
    if profile is None:
        await callback.answer("Сначала соберите план", show_alert=True)
        return
    profile = replace(profile, workout_days="", workout_time="")
    await profiles.save(profile)
    if callback.message:
        try:
            await callback.message.edit_text(_remind_text(profile), reply_markup=_remind_markup(profile))
        except Exception:  # noqa: BLE001
            pass
    await callback.answer("Напоминания выключены")


async def _send_workout_reminders(bot: Bot) -> None:
    tz = ZoneInfo(ctx.settings.timezone)
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    weekday = now.isoweekday()
    hhmm = now.strftime("%H:%M")

    for profile in await profiles.with_reminders():
        if weekday not in profile.weekdays:
            continue
        try:
            hour, minute = (int(x) for x in profile.workout_time.split(":"))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, AttributeError):
            continue
        # Допускаем опоздание до 30 минут: цикл может проснуться не в ту секунду
        if now < scheduled or (now - scheduled) > timedelta(minutes=30):
            continue
        if profile.last_reminder == today:
            continue
        if not await subs.has_access(profile.user_id):
            continue

        plan = planner.build(profile)
        if not plan.days:
            continue
        index = profile.weekdays.index(weekday) % len(plan.days)
        day = plan.days[index]
        weights = await _plan_weights(profile.user_id, profile, plan)

        lines = [f"<b>Сегодня: день {index + 1} · {day.title}</b>", ""]
        for n, slot in enumerate(day.slots, 1):
            target = weights.get(slot.exercise.slug) or {}
            suffix = f" · {target['label']}" if target.get("label") else ""
            reps_text = target.get("reps") or slot.reps
            lines.append(f"{n}. {slot.exercise.name_ru} — {slot.sets}×{reps_text}{suffix}")
        lines.append("")
        lines.append("<i>Отметьте результат после тренировки — тогда прогрессия сработает.</i>")

        markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Всё по плану", callback_data=f"wk:done:{index}"),
                InlineKeyboardButton(text="✍️ Детали", callback_data=f"wk:day:{index}"),
                InlineKeyboardButton(text="🟡 Частично", callback_data=f"wk:part:{index}"),
                InlineKeyboardButton(text="😕 Не смог", callback_data=f"wk:none:{index}"),
            ]]
        )
        try:
            await bot.send_message(profile.user_id, "\n".join(lines), reply_markup=markup)
        except Exception:  # noqa: BLE001
            logger.warning("Не смог отправить напоминание о тренировке %s", profile.user_id)
            continue
        await profiles.mark_reminder(profile.user_id, today)
        logger.info("Напоминание о тренировке отправлено: %s", profile.user_id)


async def _workout_loop(bot: Bot) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await _send_workout_reminders(bot)
        except Exception:  # noqa: BLE001
            logger.exception("Сбой в цикле напоминаний о тренировках")
        await asyncio.sleep(60)


@pay_router.message(F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_TEXTS))
async def on_unknown(message: Message) -> None:
    """Сообщение без команды раньше просто игнорировалось («is not handled»).

    Отвечаем только в личке: в канале такой ответ был бы мусором в ленте.
    """
    if message.chat.type != "private" or message.from_user is None or message.from_user.is_bot:
        return
    if message.from_user and _is_owner(message.from_user.id):
        hint = (
            "Команды:\n"
            "/post — пост на очередную тему\n"
            "/topic &lt;тема&gt; — пост на свою тему\n"
            "/sub — подписка и оплата\n"
            "/backup — бэкап базы\n"
            "/help — справка"
        )
    else:
        hint = (
            "Я работаю по командам:\n"
            "/sub — подписка и бесплатный пробный период\n"
            "/help — справка"
        )
    await message.answer(hint)


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Глобальный перехват: логируем и не даём боту упасть."""
    logger.error("Необработанная ошибка апдейта: %s", event.exception, exc_info=event.exception)
    return True


# ---------------------------------------------------------------------------
# Подписка: триал и оплата Stars
# ---------------------------------------------------------------------------
def _sub_screen(status: "subs.Status") -> tuple:
    """Текст и кнопки экрана подписки для текущего статуса."""
    lines = ["<b>Подписка</b>", ""]

    if status.kind == "paid":
        lines.append(f"Доступ активен до {status.paid_until:%d.%m.%Y}, осталось {status.days_left} дн.")
    elif status.kind == "trial":
        lines.append(f"Пробный период до {status.trial_ends_at:%d.%m.%Y}, осталось {status.days_left} дн.")
    elif status.kind == "expired":
        lines.append("Срок доступа закончился. План и дневник сохранены, доступны только для чтения.")
    else:
        lines.append("Доступ не активирован.")

    rows = []
    if not status.trial_used and not status.active:
        rows.append([
            InlineKeyboardButton(
                text=f"Попробовать {subs.TRIAL_DAYS} дней бесплатно",
                callback_data="sub:trial",
            )
        ])
    # Ручная оплата переводом: доступна, если заданы реквизиты или QR
    if orders.PAYMENT_MODE == "card" and orders.CARD_NUMBER:
        manual = True
    else:
        manual = bool(orders.PAYMENT_LINK or orders.qr_file())
    action = "Продлить" if status.active else "Оформить"

    if manual:
        rows.append([
            InlineKeyboardButton(
                text=f"{action} на {subs.SUBSCRIPTION_DAYS} дней · {orders.PRICE_RUB:.0f} ₽",
                callback_data="order:new",
            )
        ])
        if status.active:
            lines.extend([
                "",
                f"<i>Оплата заранее добавляет {subs.SUBSCRIPTION_DAYS} дней "
                "к текущей дате окончания, ничего не сгорает.</i>",
            ])

    if subs.SALES_ENABLED:
        rows.append([
            InlineKeyboardButton(
                text=f"Оплатить {subs.PRICE_STARS} ⭐ за {subs.SUBSCRIPTION_DAYS} дней",
                callback_data="sub:pay",
            )
        ])
    elif not manual:
        lines.extend(["", "<i>Оплата откроется вместе с запуском платных функций.</i>"])

    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return "\n".join(lines), markup


@pay_router.message(Command("sub"))
async def cmd_sub(message: Message) -> None:
    status = await subs.get_status(message.from_user.id)
    text, markup = _sub_screen(status)
    await message.answer(text, reply_markup=markup)


@pay_router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    contact = os.getenv("SUPPORT_CONTACT", "").strip() or "владельцу бота"
    await message.answer(f"Вопросы по оплате и возвратам: {contact}")


@pay_router.callback_query(F.data == "sub:trial")
async def cb_sub_trial(callback: CallbackQuery) -> None:
    try:
        status = await subs.start_trial(callback.from_user.id)
        await callback.answer(f"Пробный период активирован на {subs.TRIAL_DAYS} дней")
    except subs.TrialAlreadyUsed:
        status = await subs.get_status(callback.from_user.id)
        await callback.answer("Пробный период уже был использован", show_alert=True)

    text, markup = _sub_screen(status)
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 — сообщение могло устареть
            logger.debug("Не удалось обновить экран подписки")


@pay_router.callback_query(F.data == "sub:pay")
async def cb_sub_pay(callback: CallbackQuery) -> None:
    if not subs.SALES_ENABLED:
        await callback.answer("Оплата ещё не включена", show_alert=True)
        return

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id if callback.message else user_id
    try:
        await callback.bot.send_invoice(
            chat_id=chat_id,
            title=f"Доступ на {subs.SUBSCRIPTION_DAYS} дней",
            description="Планы тренировок, питание и техника упражнений",
            payload=f"sub:{user_id}:{subs.SUBSCRIPTION_DAYS}",
            provider_token="",       # цифровые товары: только Stars
            currency="XTR",
            prices=[LabeledPrice(label=f"{subs.SUBSCRIPTION_DAYS} дней", amount=subs.PRICE_STARS)],
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось выставить счёт на подписку")
        await callback.answer("Не получилось выставить счёт, попробуйте позже", show_alert=True)
        return
    await callback.answer()


@pay_router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Подтверждение обязательно: без него оплата зависает."""
    await query.answer(ok=True)


@pay_router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    payment = message.successful_payment
    days = subs.SUBSCRIPTION_DAYS

    payload = payment.invoice_payload or ""
    parts = payload.split(":")
    if len(parts) == 3 and parts[0] == "sub":
        try:
            days = int(parts[2])
        except ValueError:
            logger.warning("Непонятный payload платежа: %r", payload)

    status = await subs.add_paid_days(
        user_id=message.from_user.id,
        days=days,
        charge_id=payment.telegram_payment_charge_id,
        amount=payment.total_amount,
    )

    until = status.paid_until.strftime("%d.%m.%Y") if status.paid_until else "—"
    await message.answer(f"Оплата прошла ✅ Доступ активен до {until}.")
    await _send_db_backup(message.bot, "Бэкап после оплаты Stars")
    logger.info(
        "Оплата: user=%s, %s ⭐, charge=%s, доступ до %s",
        message.from_user.id, payment.total_amount, payment.telegram_payment_charge_id, until,
    )


# ---------------------------------------------------------------------------
# Заявки на оплату переводом (QR СБП или карта)
# ---------------------------------------------------------------------------
def _order_text(order: "orders.Order") -> str:
    tz = ZoneInfo(ctx.settings.timezone)
    until = order.expires_at.astimezone(tz).strftime("%H:%M")
    lines = [
        f"<b>Заказ #{order.short}</b>",
        "",
        f"Сумма: <b>{order.amount_str} ₽</b> (переведите ровно столько)",
        f"Заявка действует до {until} по МСК, потом её нужно оформлять заново.",
        "",
    ]
    if orders.PAYMENT_MODE == "card" and orders.CARD_NUMBER:
        lines += [
            "Перевод на карту:",
            f"<code>{html.escape(orders.CARD_NUMBER)}</code>",
            f"Получатель: {html.escape(orders.CARD_HOLDER or '—')}",
        ]
        if orders.PAYMENT_LINK:
            lines += ["", f"Либо по ссылке: {html.escape(orders.PAYMENT_LINK)}"]
    else:
        if orders.PAYMENT_LINK:
            lines.append("Оплата по СБП: нажмите кнопку «Оплатить по СБП» ниже.")
        else:
            lines.append("Оплата по СБП: отсканируйте QR ниже.")
    lines += ["", "Копейки в сумме — это номер заказа, по ним я найду ваш перевод."]
    return "\n".join(lines)


def _order_keyboard(order: "orders.Order") -> InlineKeyboardMarkup:
    rows = []
    # Ссылку СБП Telegram открывает в приложении банка — картинка не обязательна
    if orders.PAYMENT_MODE != "card" and orders.PAYMENT_LINK:
        rows.append([InlineKeyboardButton(text="💳 Оплатить по СБП", url=orders.PAYMENT_LINK)])
    rows.append([InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"order:paid:{order.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _notify_owner(order: "orders.Order", user, bot: Bot) -> None:
    """Отправляет владельцу заявку на подтверждение."""
    owners = ctx.settings.owner_ids
    if not owners:
        logger.warning("OWNER_ID не задан — заявку #%s некому подтверждать", order.short)
        return

    who = f"@{user.username}" if user.username else user.full_name
    text = (
        f"💰 <b>Заявка #{order.short}</b>\n"
        f"Сумма: <b>{order.amount_str} ₽</b>\n"
        f"Пользователь: {html.escape(who)} (id {order.user_id})\n"
        f"Срок: {order.days} дн.\n\n"
        "Проверьте перевод в выписке и подтвердите."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"order:ok:{order.id}"),
            InlineKeyboardButton(text="✖️ Отклонить", callback_data=f"order:no:{order.id}"),
        ]]
    )
    for owner_id in owners:
        try:
            await bot.send_message(owner_id, text, reply_markup=markup)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить заявку владельцу %s", owner_id)


async def _send_db_backup(bot: Bot, note: str = "") -> None:
    """Отправляет копию базы владельцу. Сбой бэкапа не должен ломать сценарий."""
    owners = ctx.settings.owner_ids
    if not owners:
        return
    try:
        path = await backup.make_backup()
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сделать бэкап базы")
        return
    if path is None:
        return
    for owner_id in owners:
        try:
            await bot.send_document(owner_id, FSInputFile(path), caption=note or "Бэкап базы")
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить бэкап владельцу %s", owner_id)


async def _send_subscription_reminders(bot: Bot) -> None:
    """Напоминает за 3 дня до конца подписки или триала."""
    try:
        items = await subs.due_reminders(3)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось получить список напоминаний")
        return

    for item in items:
        until = item["until"].strftime("%d.%m")
        if item["kind"] == "paid":
            text = (
                f"Подписка заканчивается {until} — осталось {item['days_left']} дн.\n"
                "Продлить можно заранее: дни добавятся к текущему сроку, ничего не сгорает."
            )
        else:
            text = (
                f"Пробный период заканчивается {until} — осталось {item['days_left']} дн.\n"
                "Оформить подписку можно в любой момент."
            )

        markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="Открыть подписку", callback_data="sub:menu")
            ]]
        )
        try:
            await bot.send_message(item["user_id"], text, reply_markup=markup)
        except Exception:  # noqa: BLE001 — пользователь мог заблокировать бота
            logger.warning("Не смог отправить напоминание %s", item["user_id"])
            continue

        await subs.mark_reminder(item["user_id"], item["kind"], item["until_iso"])
        logger.info("Напоминание отправлено: user=%s, %s", item["user_id"], item["kind"])


async def _reminder_loop(bot: Bot) -> None:
    await asyncio.sleep(60)  # даём боту подняться
    while True:
        await _send_subscription_reminders(bot)
        await asyncio.sleep(6 * 60 * 60)


async def _daily_backup(bot: Bot) -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        await _send_db_backup(bot, "Ежедневный бэкап базы")


@pay_router.callback_query(F.data == "order:new")
async def cb_order_new(callback: CallbackQuery) -> None:
    already = False
    try:
        order = await orders.create_order(callback.from_user.id, subs.SUBSCRIPTION_DAYS)
        already = False
    except orders.OrderExists as exc:
        order = exc.order
        already = True
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось создать заявку")
        await callback.answer("Не получилось создать заявку, попробуйте позже", show_alert=True)
        return

    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    text = _order_text(order)
    try:
        qr = orders.qr_file()
        if orders.PAYMENT_MODE != "card" and qr is not None:
            await callback.bot.send_photo(
                chat_id,
                FSInputFile(qr),
                caption=text,
                reply_markup=_order_keyboard(order),
            )
        else:
            await callback.bot.send_message(chat_id, text, reply_markup=_order_keyboard(order))
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить реквизиты")
        await callback.answer("Не получилось отправить реквизиты", show_alert=True)
        return
    if already:
        await callback.answer("У вас уже есть активная заявка", show_alert=True)
    else:
        await callback.answer()


@pay_router.callback_query(F.data.startswith("order:paid:"))
async def cb_order_paid(callback: CallbackQuery) -> None:
    try:
        order_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return

    current = await orders.get(order_id)
    if current is not None and current.status == orders.STATUS_WAITING:
        await callback.answer("Заявка уже на проверке, ждите подтверждения", show_alert=True)
        return

    try:
        order = await orders.mark_paid(order_id, callback.from_user.id)
    except orders.OrderExpired:
        await callback.answer("Заявка истекла. Оформите новую: /sub", show_alert=True)
        return
    except orders.OrderNotFound:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if order.status == orders.STATUS_PAID:
        await callback.answer("Эта заявка уже подтверждена", show_alert=True)
        return

    await callback.answer("Передал на проверку, доступ открою после подтверждения")
    await _notify_owner(order, callback.from_user, callback.bot)


@router.callback_query(F.data.startswith("order:ok:"))
async def cb_order_approve(callback: CallbackQuery) -> None:
    order_id = int((callback.data or "").split(":")[2])
    order = await orders.get(order_id)
    if order is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if order.status == orders.STATUS_PAID:
        await callback.answer("Заявка уже подтверждена", show_alert=True)
        return
    if order.status == orders.STATUS_REJECTED:
        await callback.answer("Заявка уже отклонена", show_alert=True)
        return

    # Сначала продлеваем доступ, потом закрываем заявку: если запись в БД
    # сорвётся, заявка останется активной и подтверждение можно повторить.
    status = await subs.add_paid_days(
        order.user_id, order.days, charge_id=f"order:{order.id}", amount=int(round(order.amount))
    )
    try:
        order = await orders.review(order_id, callback.from_user.id, approve=True)
    except (orders.OrderNotFound, orders.OrderExpired) as exc:
        await callback.answer(f"Доступ выдан, но заявку закрыть не вышло: {exc}", show_alert=True)
        return
    until = status.paid_until.strftime("%d.%m.%Y") if status.paid_until else "—"

    try:
        await callback.bot.send_message(
            order.user_id, f"Оплата подтверждена ✅ Доступ активен до {until}."
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не смог уведомить пользователя %s", order.user_id)

    if callback.message:
        try:
            await callback.message.edit_text(f"Заявка #{order.short} подтверждена, доступ до {until}.")
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось обновить сообщение заявки")

    await _send_db_backup(callback.bot, f"Бэкап после оплаты #{order.short}")
    await callback.answer("Подтверждено")


@router.callback_query(F.data.startswith("order:no:"))
async def cb_order_reject(callback: CallbackQuery) -> None:
    order_id = int((callback.data or "").split(":")[2])
    try:
        order = await orders.review(order_id, callback.from_user.id, approve=False)
    except (orders.OrderNotFound, orders.OrderExpired) as exc:
        await callback.answer(f"Нельзя отклонить: {exc}", show_alert=True)
        return

    try:
        await callback.bot.send_message(
            order.user_id,
            f"Заявка #{order.short} отклонена: платёж не найден.\n"
            "Если вы точно переводили — напишите в поддержку.",
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не смог уведомить пользователя %s", order.user_id)

    if callback.message:
        try:
            await callback.message.edit_text(f"Заявка #{order.short} отклонена.")
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось обновить сообщение заявки")
    await callback.answer("Отклонено")


# ---------------------------------------------------------------------------
# Бэкап и восстановление базы
# ---------------------------------------------------------------------------
@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    """Ручной бэкап: присылает файл базы в этот чат."""
    path = await backup.make_backup()
    if path is None:
        await message.answer("База не найдена — бэкапить нечего.")
        return
    await message.answer_document(FSInputFile(path), caption="Бэкап базы fitbot.db")


@router.message(F.document)
async def on_db_restore(message: Message) -> None:
    """Восстановление базы: перешлите боту файл .db."""
    document = message.document
    if not (document.file_name or "").lower().endswith(".db"):
        await message.answer("Нужен файл базы (.db).")
        return

    buffer = await message.bot.download(document)
    try:
        previous = await backup.restore(buffer.read())
    except ValueError as exc:
        await message.answer(f"Не восстановил: {exc}")
        return

    await message.answer(
        f"База восстановлена ✅ Прежняя сохранена как <code>{previous.name}</code>.\n"
        "Перезапустите бота, чтобы сбросить возможные кеши."
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
async def _set_commands(bot: Bot) -> None:
    """Публичное меню — всем; админские команды — только владельцу."""
    public = [
        BotCommand(command="start", description="Начать"),
        BotCommand(command="plan", description="Мой план тренировок"),
        BotCommand(command="calories", description="Калории и БЖУ"),
        BotCommand(command="diary", description="Дневник питания"),
        BotCommand(command="progress", description="Прогресс тренировок"),
        BotCommand(command="remind", description="Напоминания о тренировках"),
        BotCommand(command="history", description="История и стаж"),
        BotCommand(command="sub", description="Подписка и пробный период"),
        BotCommand(command="help", description="Справка"),
    ]
    admin = public + [
        BotCommand(command="post", description="Пост на очередную тему"),
        BotCommand(command="topic", description="Пост на заданную тему"),
        BotCommand(command="backup", description="Бэкап базы"),
    ]
    try:
        await bot.set_my_commands(public)
        for owner_id in ctx.settings.owner_ids:
            try:
                await bot.set_my_commands(
                    admin, scope=BotCommandScopeChat(chat_id=owner_id)
                )
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось выставить команды владельцу %s", owner_id)
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось обновить список команд бота")


async def main() -> None:
    # Логи настраиваем до load_settings(), чтобы предупреждения конфига попали в вывод
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    settings = load_settings()
    log_build_info()  # маркер сборки: только теперь у logging есть обработчик
    if settings.owner_ids:
        logger.info("Доступ ограничен владельцем: %s", ", ".join(str(i) for i in settings.owner_ids))
    else:
        logger.error(
            "OWNER_ID не задан — бот откажет всем. Напишите боту любое сообщение, "
            "его id появится в логе в строке «Доступ запрещён»."
        )
    logger.info("Канал для публикации: %s (как пришло из панели: %s)",
                settings.channel_id, os.getenv("CHANNEL_ID", "не задан"))

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(pay_router)   # оплата и /sub — всем
    dispatcher.include_router(router)

    # Ограничение доступа: админские команды только владельцу.
    # Вешаем на роутер, а не на весь Dispatcher, иначе не пройдут платежи.
    router.message.outer_middleware(OwnerOnlyMiddleware())
    router.callback_query.outer_middleware(OwnerOnlyMiddleware())

    ctx.settings = settings
    ctx.ai = DeepSeekClient(settings)
    ctx.rotation = TopicRotation(TOPIC_ROTATION)

    await _set_commands(bot)

    # Кнопка меню в Telegram открывает мини-апп (если задан публичный URL)
    if settings.webapp_url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Приложение", web_app=WebAppInfo(url=settings.webapp_url)
                )
            )
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось установить кнопку меню мини-аппа")

    # База пропала (новый контейнер) — предупреждаем владельца, но не падаем
    if not backup.DB_PATH.exists() and settings.owner_ids:
        for owner_id in settings.owner_ids:
            try:
                await bot.send_message(
                    owner_id,
                    "База данных не найдена. Если это не первый запуск — "
                    "перешлите боту последний файл <code>.db</code>, он восстановит подписки.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("Не смог предупредить владельца %s о пустой базе", owner_id)

    # Проверяем канал сразу на старте: иначе «chat not found» всплывёт
    # только в момент автопоста, например в 07:00.
    if settings.channel_id:
        try:
            chat = await bot.get_chat(settings.channel_id)
            logger.info("Канал доступен: %s (@%s), id=%s", chat.title, chat.username, chat.id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Бот не видит канал %s: %s. Проверьте CHANNEL_ID в панели "
                "(нужен @username или -100…) и права администратора.",
                settings.channel_id, exc,
            )

    logger.info(
        "Мини-апп: порт=%s, URL=%s",
        settings.webapp_port or "выключен (WEBAPP_PORT=0)",
        settings.webapp_url or "не задан (WEBAPP_URL пуст)",
    )

    qr = orders.qr_file()
    logger.info(
        "Оплата переводом: режим=%s, ссылка=%s, QR=%s, цена=%s руб, TTL=%s мин",
        orders.PAYMENT_MODE,
        "задана" if orders.PAYMENT_LINK else "НЕ ЗАДАНА",
        qr.name if qr else "файла нет",
        orders.PRICE_RUB,
        orders.ORDER_TTL_MINUTES,
    )

    scheduler = None
    if settings.autopost_enabled:
        if settings.channel_id:
            async def scheduled_job() -> None:
                await autopost(bot)

            scheduler = create_scheduler(settings, scheduled_job)
            scheduler.start()
            logger.info("Автопостинг включён: %s", describe_schedule(settings))
            for job in scheduler.get_jobs():
                logger.info("Задача %s, следующий запуск: %s", job.id, job.next_run_time)
        else:
            logger.warning("AUTOPOST_ENABLED=true, но CHANNEL_ID не задан — автопостинг выключен")

    backup_task = asyncio.create_task(_daily_backup(bot))
    reminder_task = asyncio.create_task(_reminder_loop(bot))
    workout_task = asyncio.create_task(_workout_loop(bot))

    if settings.webapp_port:
        try:
            await webapp_api.start(settings)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось запустить мини-апп на порту %s", settings.webapp_port)

    try:
        logger.info("Бот запущен, жду команды")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        backup_task.cancel()
        reminder_task.cancel()
        workout_task.cancel()
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await ctx.ai.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    _load_pending()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка по Ctrl+C")
