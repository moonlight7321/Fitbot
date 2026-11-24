"""Конфигурация проекта: переменные окружения (.env) и системные промты.

Секреты в коде не хранятся: всё берётся из окружения через python-dotenv.
Обязательные переменные проверяются на старте, чтобы бот падал сразу
с понятным сообщением, а не в момент первого запроса к API.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# override=False: настоящие переменные окружения приоритетнее файла .env
load_dotenv(override=False)


# ---------------------------------------------------------------------------
# Системный промт для генерации: роль, стиль, жёсткие запреты и три блока.
# Вынесен сюда, чтобы его можно было править без правки кода бота.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты — контент-редактор Telegram-канала о спорте и правильном питании.
Аудитория: 18–35 лет, тренируются (в том числе кроссфит), следят за питанием,
ценят короткие и реалистичные решения. Часто работают много и устают.

ТОН: дружелюбный, мотивирующий, энергичный, но без «инфоцыганства» и давления.
На «ты», короткими абзацами, 1–3 эмодзи на пост.

ЖЁСТКИЕ ПРАВИЛА:
1. Блок [ПОСТ] — строго 250–400 символов (с заголовком, призывом и хэштегами).
2. Структура поста — аккуратная вёрстка для Telegram:
   <b>🔥 Заголовок с эмодзи</b>
   пустая строка, 1–3 коротких абзаца; при желании список из 2–3 пунктов с «▫️»;
   <i>призыв к действию</i> (вопрос аудитории или «напишите в комментариях…»);
   пустая строка, 1–2 тематических хэштега.
   Разрешены только теги <b>, <i>, <u>, <s>, <blockquote>. Никаких других тегов,
   атрибутов и markdown. Служебные подписи «Заголовок:», «Текст:» не пиши — сразу вёрстка.
3. Опирайся на научно обоснованные принципы: постепенный прогресс, регулярность,
   восстановление, баланс нагрузки и питания.
4. Запрещено: обещания «минус 10 кг за неделю» и любой гарантированный результат,
   жёсткие диеты и «сушки», чудо-средства и добавки, конкретные медицинские назначения,
   советы при боли, травмах и заболеваниях.
5. Питание — только общие принципы (порция, замена продукта, планирование),
   без индивидуальных планов и расчётов «на глаз».
6. Тренировки — варианты разной сложности (новичок / средний / продвинутый)
   и напоминание о безопасности: разминка, техника, без работы «через боль».
7. Если тема спорная — смещай акцент на безопасный бытовой микрошаг.
8. Если в запросе задан ФОРМАТ (чек-лист, миф, инструкция, сигналы тела) — следуй ему;
   иначе используй обычную структуру абзацев.
9. Темы про сигналы тела и боль — только информационно: возможные причины, что сделать
   в ближайшие дни и когда обратиться к врачу. Никаких диагнозов, лечения и «помажь
   и продолжай».

ФОРМАТ ОТВЕТА — ровно три блока, каждый начинается с заголовка в квадратных скобках:

[ПОСТ]
<b>🔥 Заголовок</b>

1–3 коротких абзаца. Если уместно — список:
▫️ пункт
▫️ пункт

<i>Призыв к действию</i>

#хэштег1 #хэштег2

[ИДЕЯ ДЛЯ КАРТИНКИ]
Промт на РУССКОМ языке для GigaChat/Kandinsky, 25–40 слов: конкретная сцена
(что именно делает человек и где), объекты, фон, настроение.
Сцена должна иллюстрировать именно тему поста: растяжка — человек тянется на коврике,
питание — еда и кухня, сигналы тела — человек прислушивается к себе, привычки — утро
и планирование. Не рисуй спортзал, если тема не про тренировку. Стиль — реалистичное
фото или полуреалистичный рендер, без мультяшности. Не пиши общих слов вроде
«красиво» и «эпично» — только конкретика. Каждый раз придумывай новую сцену,
не повторяй формулировки предыдущих постов. Без текста и надписей на картинке,
без логотипов.

[ПРОВЕРКА]
- Логика: ...
- Факты: ...
- Безопасность: ...
- Стиль и длина: ...
- Призыв и хэштеги: ...

Выводи только эти три блока, без вступлений и комментариев вокруг них."""


# ---------------------------------------------------------------------------
# Промт для независимой проверки поста (второй запрос к DeepSeek).
# ---------------------------------------------------------------------------
REVIEW_SYSTEM_PROMPT = """Ты — строгий редактор-фактчекер Telegram-канала о спорте и питании.
Тебе дают готовый пост. Проверь его и верни короткий разбор.

Проверяй:
1. Логику и противоречия внутри текста.
2. Фактические ошибки и ложные обещания результата.
3. Опасные советы: экстремальные диеты, «сушки», чудо-средства, тренировки
   «до отказа» без разминки, любые советы при боли и травмах.
4. Опечатки, битую пунктуацию, лишние пробелы.
5. Формат: заголовок, призыв к действию, 1–2 хэштега, объём 250–400 символов.

ФОРМАТ ОТВЕТА (строго):
Первая строка — «СТАТУС: OK», если пост можно публиковать, или
«СТАТУС: ПРОБЛЕМА», если есть хотя бы одна серьёзная ошибка.
Далее 3–5 пунктов списка: что проверено и что именно не так.
Пиши по-русски, без воды, не длиннее 600 символов."""


@dataclass(frozen=True)
class Settings:
    """Все настройки приложения."""

    telegram_token: str
    deepseek_api_key: str
    channel_id: Optional[str]
    deepseek_model: str
    deepseek_base_url: str
    request_timeout: float
    post_min_chars: int
    post_max_chars: int
    image_width: int
    image_height: int
    image_timeout: float
    autopost_enabled: bool
    owner_ids: Tuple[int, ...]
    post_interval_hours: float
    post_times: Tuple[Tuple[int, int], ...]
    timezone: str
    webapp_url: Optional[str]
    webapp_port: int


def _env_str(name: str, default: str = "") -> str:
    """Строка из окружения; пустое значение считается отсутствующим."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r — не число, использую значение по умолчанию %s", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _env_str(name).replace(",", ".")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r — не число, использую значение по умолчанию %s", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "да"}


def _parse_ids(raw: str) -> Tuple[int, ...]:
    """Список Telegram id владельцев из «123, 456» (или ADMIN_IDS)."""
    result = []
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip().lstrip("@")
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            logger.warning("OWNER_ID: «%s» — не число, пропускаю", chunk)
    return tuple(result)


def _normalize_url(value: str) -> str:
    """Приводит публичный адрес мини-аппа к виду https://host/."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    if raw.lower().startswith("http://"):
        raw = "https://" + raw[7:]
    return raw.rstrip("/") + "/"


def _normalize_channel(value: str) -> str:
    """Приводит CHANNEL_ID к виду, который понимает Bot API.

    Панель хостинга любит ссылки: «t.me/Sport_PP7321» или
    «https://t.me/Sport_PP7321» — Bot API на такое отвечает «chat not found».
    Числовые id вида -1001234567890 оставляем как есть.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            raw = raw[len(prefix):]
            lowered = raw.lower()
            break
    for prefix in ("t.me/", "telegram.me/", "telegram.dog/"):
        if lowered.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip("/")
    if not raw:
        return ""
    if raw.startswith("@"):
        return raw
    if raw.lstrip("-").isdigit():   # -100... — числовой id канала
        return raw
    return "@" + raw


def _parse_times(raw: str) -> Tuple[Tuple[int, int], ...]:
    """Разбирает POST_TIMES вида «09:00, 19:30» в кортеж пар (час, минута)."""
    result = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hours_text, minutes_text = chunk.split(":", 1)
            hour, minute = int(hours_text), int(minutes_text)
        except ValueError as exc:
            raise ValueError(
                f"Некорректное время в POST_TIMES: «{chunk}». "
                f"Ожидается формат HH:MM, например 09:00,19:00"
            ) from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Время вне диапазона в POST_TIMES: «{chunk}»")
        result.append((hour, minute))
    return tuple(sorted(set(result)))


def load_settings() -> Settings:
    """Читает .env и возвращает настройки или падает с понятной ошибкой."""
    token = _env_str("TELEGRAM_TOKEN")
    deepseek_key = _env_str("DEEPSEEK_API_KEY")

    missing = [
        name
        for name, value in (("TELEGRAM_TOKEN", token), ("DEEPSEEK_API_KEY", deepseek_key))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения: "
            + ", ".join(missing)
            + ". Скопируйте .env.example в .env и заполните значения."
        )

    post_min = _env_int("POST_MIN_CHARS", 250, 50, 2000)
    post_max = _env_int("POST_MAX_CHARS", 400, post_min + 50, 4000)

    return Settings(
        telegram_token=token,
        deepseek_api_key=deepseek_key,
        channel_id=_normalize_channel(_env_str("CHANNEL_ID")) or None,
        deepseek_model=_env_str("DEEPSEEK_MODEL", "deepseek-chat"),
        deepseek_base_url=_env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        request_timeout=_env_float("REQUEST_TIMEOUT", 90.0, 10.0, 300.0),
        post_min_chars=post_min,
        post_max_chars=post_max,
        image_width=_env_int("IMAGE_WIDTH", 1024, 256, 2048),
        image_height=_env_int("IMAGE_HEIGHT", 1024, 256, 2048),
        image_timeout=_env_float("IMAGE_TIMEOUT", 90.0, 10.0, 300.0),
        autopost_enabled=_env_bool("AUTOPOST_ENABLED", True),
        # Пусто -> бот отвечает отказом всем; id владельца видно в логе после сообщения боту
        owner_ids=_parse_ids(_env_str("OWNER_ID") or _env_str("ADMIN_IDS")),
        # Дефолт — раз в 6 часов; минимум 15 минут, чтобы не спамить канал
        post_interval_hours=_env_float("POST_INTERVAL_HOURS", 6.0, 0.25, 168.0),
        post_times=_parse_times(_env_str("POST_TIMES")),
        timezone=_env_str("TIMEZONE", "Europe/Moscow"),
        webapp_url=_normalize_url(_env_str("WEBAPP_URL")) or None,
        # Публичный HTTPS-адрес мини-аппа (кнопка в Telegram) и локальный порт,
        # на котором поднимается aiohttp-сервер внутри процесса бота
        # Порт мини-аппа: WEBAPP_PORT, иначе PORT (его выставляют PaaS-хостинги)
        webapp_port=_env_int("WEBAPP_PORT", _env_int("PORT", 0, 0, 65535), 0, 65535),
    )
