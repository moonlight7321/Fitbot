"""Генерация картинок через Kandinsky внутри GigaChat API."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from gigachat import GigaChat

logger = logging.getLogger(__name__)

AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
# Модель и scope вынесены в .env: на другом тарифе/аккаунте они отличаются
MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-3-Ultra")
SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

# Если модель не прислала промт картинки — используем нейтральный дефолт
FALLBACK_PROMPT = (
    "Нарисуй реалистичное фото: атлет тренируется в светлом минималистичном "
    "спортзале, естественный свет, чистый фон, энергичное спокойное настроение, "
    "без текста и надписей"
)


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
    text = (prompt or "").strip() or FALLBACK_PROMPT
    if not AUTH_KEY:
        logger.error("Не задан GIGACHAT_AUTH_KEY в .env")
        return None
    try:
        data = await asyncio.to_thread(_generate_sync, text)
        logger.info("Картинка получена: %s КБ", len(data) // 1024)
        return data
    except Exception as exc:  # noqa: BLE001 — сеть, API, формат ответа
        logger.warning("Картинка не получена: %s", exc)
        return None
