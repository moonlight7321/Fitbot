"""Генерация картинок через Kandinsky внутри GigaChat API.

Одна и та же сцена каждый раз выглядела одинаково, поэтому к промту модели
добавляется случайная «раскадровка»: ракурс, свет, палитра, формат кадра.
Стили ограничены реалистичной подачей — канал держит единый визуальный стиль,
поэтому аниме/комикс/скетч сюда сознательно не входят.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

from gigachat import GigaChat

logger = logging.getLogger(__name__)

AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
# Модель и scope вынесены в .env: на другом тарифе/аккаунте они отличаются
MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-3-Ultra")
SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
# Дефолтные 30 с из SDK обрывают чтение: Kandinsky рисует 30–60 с
TIMEOUT = float(os.getenv("GIGACHAT_TIMEOUT", "120"))
RETRIES = int(os.getenv("GIGACHAT_RETRIES", "1"))

# Если модель не прислала промт картинки — используем нейтральный дефолт
FALLBACK_PROMPT = (
    "атлет тренируется в светлом минималистичном спортзале, естественный свет, "
    "чистый фон, спокойная уверенность"
)

# --- Вариации. Kandinsky хорошо реагирует на конкретику, а не на «красиво» ---
ANGLES: List[str] = [
    "крупный план, акцент на движении",
    "средний план, всё тело в кадре",
    "вид сбоку, профиль, динамичная поза",
    "ракурс чуть снизу, подчёркнутая устойчивость",
    "вид сверху, геометрия пространства",
    "кадр через плечо, взгляд в зеркало",
    "фрагмент: кисти рук и гриф, детали хвата",
]

LIGHTS: List[str] = [
    "мягкий дневной свет из большого окна",
    "тёплый вечерний свет, длинные тени",
    "холодный студийный свет, чистые блики",
    "контровый свет, силуэт и объём",
    "рассеянный свет пасмурного утра",
    "тёплые лампы зала, приглушённый фон",
]

PALETTES: List[str] = [
    "спокойная палитра: серый, белый, охра",
    "тёплые тона: терракота и песок",
    "холодные тона: графит и сталь",
    "приглушённый зелёный и беж",
    "монохром с одним цветовым акцентом",
]

FORMATS: List[str] = [
    "реалистичное фото, объектив 35 мм",
    "реалистичное фото, 50 мм, малая глубина резкости",
    "полуреалистичный цифровой рендер",
    "журнальная съёмка, естественные текстуры",
    "кадр из документального фильма о спорте",
]

NO_TEXT = "без текста, без надписей, без логотипов, без мультяшности"

MAX_PROMPT_CHARS = 400   # длиннее Kandinsky начинает терять детали
MAX_BASE_CHARS = 200

_last_combo: Tuple[str, ...] = ()


def _trim_scene(scene: str, limit: int) -> str:
    """Обрезает сцену по границе слова: промт не должен кончаться на «сосредо»."""
    if len(scene) <= limit:
        return scene
    cut = scene[:limit]
    boundary = max(cut.rfind(" "), cut.rfind(","), cut.rfind(";"), cut.rfind("."))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:.-")


def build_prompt(base: str) -> str:
    """Собирает промт: сцена от модели + случайные ракурс, свет, палитра, формат.

    Комбинацию стараемся не повторять подряд, иначе два поста подряд
    получают визуально одинаковые картинки.
    """
    global _last_combo

    scene = (base or "").strip().rstrip(" .;,") or FALLBACK_PROMPT
    scene = _trim_scene(scene, MAX_BASE_CHARS)

    combo: Tuple[str, ...] = ()
    for _ in range(6):
        parts = [
            random.choice(ANGLES),
            random.choice(LIGHTS),
            random.choice(PALETTES),
            random.choice(FORMATS),
        ]
        combo = tuple(parts)
        if combo != _last_combo:
            break
    _last_combo = combo

    prompt = ", ".join([scene, *combo, NO_TEXT])
    if len(prompt) > MAX_PROMPT_CHARS:
        # режем сцену, а не модификаторы: они и дают разнообразие
        overflow = len(prompt) - MAX_PROMPT_CHARS
        scene = _trim_scene(scene, max(60, len(scene) - overflow))
        prompt = ", ".join([scene.rstrip(" ,"), *combo, NO_TEXT])
    return prompt


def _extract_file_id(text: str) -> str:
    match = re.search(r'<img src="([^"]+)"', text or "")
    if not match:
        raise RuntimeError("GigaChat не вернул картинку в ответе")
    return match.group(1)


def _generate_sync(prompt: str) -> bytes:
    with GigaChat(
        credentials=AUTH_KEY,
        model=MODEL,
        scope=SCOPE,
        timeout=TIMEOUT,        # иначе чтение обрывается на 30-й секунде
        max_retries=RETRIES,
        verify_ssl_certs=False,  # у GigaChat собственный УЦ, без этого SSL падает
    ) as gc:
        response = gc.chat(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": f"Нарисуй: {prompt}"}],
                "function_call": "auto",
            }
        )
        file_id = _extract_file_id(response.choices[0].message.content)
        image = gc.get_image(file_id)
        return image.content


async def fetch_image(prompt: str, settings) -> Optional[bytes]:
    """Возвращает байты картинки или None — пост уйдёт и без изображения."""
    text = build_prompt(prompt)
    if not AUTH_KEY:
        logger.error("Не задан GIGACHAT_AUTH_KEY в .env")
        return None
    logger.info("Промт картинки: %s", text)
    try:
        data = await asyncio.to_thread(_generate_sync, text)
        logger.info("Картинка получена: %s КБ", len(data) // 1024)
        return data
    except Exception as exc:  # noqa: BLE001 — сеть, API, формат ответа
        logger.warning("Картинка не получена: %s", exc)
        return None
