"""Работа с DeepSeek (OpenAI-совместимый API) и разбор ответа модели.

DeepSeek не генерирует изображения, поэтому модель отдаёт английский промт,
а картинку получает image_gen.py (Pollinations).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

from config import REVIEW_SYSTEM_PROMPT, SYSTEM_PROMPT, Settings

logger = logging.getLogger(__name__)


class AIError(RuntimeError):
    """Понятная человеку ошибка: сеть, таймаут, пустой или битый ответ модели."""


@dataclass(frozen=True)
class ReviewResult:
    """Результат независимой проверки поста (второй запрос)."""

    raw: str
    ok: bool


# Заголовок секции: [ПОСТ], **[ИДЕЯ ДЛЯ КАРТИНКИ]**, [Проверка] и т.п.
_HEADER_RE = re.compile(r"[\[【]\s*([^\]】\n]{1,60}?)\s*[\]】]")

# Варианты написания заголовков, которые встречаются у модели
_HEADER_ALIASES: Dict[str, str] = {
    "пост": "post",
    "текст поста": "post",
    "post": "post",
    "text": "post",
    "идея для картинки": "image",
    "идея картинки": "image",
    "промт для картинки": "image",
    "промпт для картинки": "image",
    "промт": "image",
    "image": "image",
    "image prompt": "image",
    "проверка": "check",
    "проверка перед публикацией": "check",
    "check": "check",
}

# Markdown-обвязка вокруг заголовков и текста: * _ ~ пробелы и переводы строк.
# Обратный апостроф добавляем отдельно, чтобы не путать редакторы и линтеры.
_WRAP_CHARS = "*_~ \t\r\n" + chr(96)

_STATUS_RE = re.compile(r"статус\s*[:：\-–—]\s*([^\n]{1,60})", re.IGNORECASE)
_OK_STATUS_WORDS = ("ok", "ок", "окей", "годен", "чисто", "принято", "хорошо", "без замечаний")
# Признаки серьёзной проблемы, если модель забыла строку «СТАТУС»
_SERIOUS_WORDS = ("серьёзн", "серьезн", "критич", "опасн", "не публиковать", "нельзя публиковать")


def _normalize_header(raw: str) -> Optional[str]:
    """Приводит заголовок секции к ключу post / image / check."""
    name = raw.strip().strip(_WRAP_CHARS).strip().lower()
    name = name.replace("ё", "е")
    name = re.sub(r"\s+", " ", name)
    name = name.rstrip(":.-–— ")
    return _HEADER_ALIASES.get(name)


def parse_sections(raw: str) -> Dict[str, str]:
    """Разбирает ответ модели на три блока.

    Устойчив к: отсутствию части секций, markdown-обвязке (**[ПОСТ]**),
    лишним/неизвестным заголовкам и тексту перед первым блоком.
    Возвращает словарь с ключами post / image / check; нет секции — пустая строка.
    """
    sections: Dict[str, str] = {"post": "", "image": "", "check": ""}
    if not raw or not raw.strip():
        return sections

    matches: List[re.Match] = list(_HEADER_RE.finditer(raw))
    if not matches:
        # Модель проигнорировала формат — считаем весь ответ постом
        logger.warning("В ответе модели нет заголовков секций, беру весь текст как пост")
        sections["post"] = raw.strip(_WRAP_CHARS)
        return sections

    preamble = raw[: matches[0].start()].strip(_WRAP_CHARS)
    for index, match in enumerate(matches):
        key = _normalize_header(match.group(1))
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = raw[body_start:body_end].strip(_WRAP_CHARS)
        if not body:
            continue
        if key is None:
            logger.debug("Неизвестный заголовок секции: %r", match.group(1))
            continue
        sections[key] = f"{sections[key]}\n{body}".strip() if sections[key] else body

    if not sections["post"] and preamble:
        # Модель написала пост без заголовка [ПОСТ]
        sections["post"] = preamble

    return sections


def clean_post(text: str) -> str:
    """Убирает служебную подпись «Заголовок:» и лишние пустые строки."""
    if not text:
        return ""
    cleaned = text.strip(_WRAP_CHARS)
    cleaned = re.sub(
        r"^\s*(?:[-*•]\s*)?(?:заголовок|тема|headline|title)\s*[:\-–—]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # «Текст:» и «Призыв:» — служебные подписи модели, в канал они попадать не должны
    cleaned = re.sub(r"(?m)^\s*текс?т\s*[:\-–—]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(?m)^\s*(?:призыв(?: к действию)?|call to action)\s*[:\-–—]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Если модель подмешала в пост служебные блоки — отрезаем их
    cleaned = re.split(r"(?mi)^\s*проверка\s*[:\-–—]", cleaned)[0].rstrip()
    cleaned = re.sub(
        r"(?m)^\s*хэштеги\s*[:\-–—]\s*", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def check_length(text: str, minimum: int, maximum: int) -> Optional[str]:
    """Возвращает описание проблемы с длиной или None, если всё в порядке.

    Считаем в символах Python (кодпоинтах): эмодзи = 1. Telegram считает
    UTF-16, но запас в диапазоне 250–400 перекрывает это расхождение.
    """
    length = len(text)
    if length < minimum:
        return f"коротко: {length} из {minimum}–{maximum} символов"
    if length > maximum:
        return f"длинно: {length} из {minimum}–{maximum} символов"
    return None


def review_failed(review: str) -> bool:
    """Определяет, нашла ли проверка серьёзную проблему."""
    if not review or not review.strip():
        return True
    match = _STATUS_RE.search(review)
    if match:
        status = match.group(1).strip().lower()
        return not any(word in status for word in _OK_STATUS_WORDS)
    lowered = review.lower()
    return any(word in lowered for word in _SERIOUS_WORDS)


class DeepSeekClient:
    """Асинхронная обёртка над DeepSeek с ретраями и понятными ошибками."""

    def __init__(self, settings: Settings) -> None:
        self._model = settings.deepseek_model
        self._client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.request_timeout,
            max_retries=2,  # openai сам повторит запрос при 429/5xx и таймаутах
        )

    async def _chat(
        self, messages: List[Dict[str, str]], *, temperature: float, max_tokens: int
    ) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except APITimeoutError as exc:
            raise AIError("DeepSeek не ответил вовремя — попробуйте через минуту") from exc
        except APIConnectionError as exc:
            raise AIError("Нет связи с DeepSeek — проверьте интернет") from exc
        except APIStatusError as exc:
            raise AIError(f"DeepSeek вернул ошибку {exc.status_code}") from exc
        except OpenAIError as exc:
            raise AIError("Не удалось получить ответ от DeepSeek") from exc

        if not response.choices:
            raise AIError("DeepSeek вернул ответ без вариантов")
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise AIError("DeepSeek вернул пустой ответ")
        return content

    async def generate_post(self, topic: str, hint: str = "", avoid: list[str] | None = None,
                            angle: str = "", repeat: int = 0) -> str:
        """Первый запрос: пост + промт картинки + самопроверка (три блока)."""
        safe_topic = (topic or "").strip()[:400] or "Свободная тема про спорт и питание"
        extra = (hint or "").strip()[:500]
        user_text = f"Тема поста: {safe_topic}\n"
        if extra:
            user_text += f"Дополнительные требования: {extra}\n"
        if repeat > 0:
            user_text += (
                f"Эта тема уже была в канале (повтор №{repeat + 1}). Не повторяй прошлый пост: "
                f"ни заголовок, ни примеры, ни структуру. Возьми другой ракурс: {angle}. "
                "В самом начале текста дай понять, что это другой разбор: начни заголовок "
                "со слов «Другой угол:» или «Продолжаем тему:».\n"
            )
        recent = [part.strip()[:220] for part in (avoid or []) if part.strip()][:5]
        if recent:
            user_text += (
                "Не повторяй эти недавние посты: ни заголовки, ни примеры, ни структуру. "
                "Найди другой угол и другие конкретные примеры:\n"
                + "\n---\n".join(recent)
                + "\n"
            )
        user_text += (
            "Сгенерируй пост по правилам и верни ответ строго в трёх блоках: "
            "[ПОСТ], [ИДЕЯ ДЛЯ КАРТИНКИ], [ПРОВЕРКА]."
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        return await self._chat(messages, temperature=0.8, max_tokens=1500)

    async def review_post(self, post_text: str) -> ReviewResult:
        """Второй запрос: независимая проверка поста перед публикацией."""
        messages = [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": f"Проверь пост перед публикацией:\n\n{post_text}"},
        ]
        review = await self._chat(messages, temperature=0.2, max_tokens=700)
        return ReviewResult(raw=review, ok=not review_failed(review))

    async def rewrite_post(self, post_text: str, instruction: str) -> str:
        """Переписывает пост по инструкции (исправление/объём) — только текст поста."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Вот текущий пост (между маркерами):\n<<<\n"
                    + post_text
                    + "\n>>>\n\n"
                    + instruction
                    + "\n\nВерни ответ в том же формате из трёх блоков "
                    "и обнови блок [ПРОВЕРКА]."
                ),
            },
        ]
        raw = await self._chat(messages, temperature=0.5, max_tokens=1500)
        sections = parse_sections(raw)
        candidate = clean_post(sections["post"])
        if not candidate:
            raise AIError("Модель вернула пустой пост при исправлении")
        return candidate

    async def close(self) -> None:
        """Аккуратно закрывает HTTP-клиент при выключении бота."""
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001 — закрытие не должно ломать выключение
            logger.debug("Не удалось корректно закрыть клиент DeepSeek")
