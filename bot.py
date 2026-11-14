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
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ai_client import AIError, DeepSeekClient, check_length, clean_post, parse_sections
from config import Settings, load_settings
from image_gen import fetch_image
from scheduler import create_scheduler, describe_schedule

logger = logging.getLogger(__name__)

router = Router()

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


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = HELP_TEXT if ctx.settings.channel_id else HELP_TEXT + NO_PUBLISH_HINT
    await message.answer(text)


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


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Глобальный перехват: логируем и не даём боту упасть."""
    logger.error("Необработанная ошибка апдейта: %s", event.exception, exc_info=event.exception)
    return True


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
async def _set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="post", description="Пост на очередную тему"),
                BotCommand(command="topic", description="Пост на заданную тему"),
                BotCommand(command="help", description="Справка"),
                BotCommand(command="start", description="Приветствие"),
            ]
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось обновить список команд бота")


async def main() -> None:
    # Логи настраиваем до load_settings(), чтобы предупреждения конфига попали в вывод
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    settings = load_settings()
    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    ctx.settings = settings
    ctx.ai = DeepSeekClient(settings)
    ctx.rotation = TopicRotation(TOPIC_ROTATION)

    await _set_commands(bot)

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

    try:
        logger.info("Бот запущен, жду команды")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
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
