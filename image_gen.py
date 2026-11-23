"""Генерация картинок через Kandinsky внутри GigaChat API.

Одна и та же сцена каждый раз выглядела одинаково, поэтому к промту модели
добавляется случайная «раскадровка»: ракурс, свет, палитра, формат кадра.
Стили ограничены реалистичной подачей — канал держит единый визуальный стиль,
поэтому аниме/комикс/скетч сюда сознательно не входят.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import re
from typing import List, Optional, Tuple

try:
    from PIL import Image, ImageStat
except ImportError:  # Pillow не установился — бот должен работать и без проверки
    Image = None
    ImageStat = None

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
# Куда ставим сцену. Привязка к направлению поста: иначе все картинки
# получаются «человек в зале» независимо от темы.
CATEGORY_SCENES = {
    "тренировка": [
        "в тренажёрном зале у стойки со штангой",
        "на площадке с турником во дворе",
        "в домашней комнате с гантелями на полу",
        "у гребного тренажёра в светлом зале",
    ],
    "растяжка": [
        "на коврике у большого окна",
        "в тихой студии йоги с деревянным полом",
        "на траве в утреннем парке",
        "дома на полу рядом с кроватью",
    ],
    "сигналы тела": [
        "дома перед зеркалом, человек прислушивается к себе",
        "на скамейке в раздевалке после тренировки",
        "на кухне с чашкой чая, задумчивый взгляд",
        "в кресле у окна с блокнотом",
    ],
    "питание": [
        "на кухне с тарелкой и овощами на столе",
        "на рынке у прилавка с зеленью",
        "за обеденным столом с простой едой",
        "у холодильника с продуктами в руках",
    ],
    "мифы": [
        "дома с ноутбуком и блокнотом на диване",
        "в кафе с чашкой кофе и телефоном",
        "на скамейке в парке с наушниками",
    ],
    "привычки": [
        "утром дома, кроссовки стоят у двери",
        "за столом с календарём и ручкой",
        "на лестнице между этажами с рюкзаком",
    ],
    "анатомия": [
        "у зеркала, человек рассматривает плечо",
        "в зале у скамьи, рука на мышце",
        "дома на полу, опора на предплечья",
    ],
}

DETAILS: List[str] = [
    "рядом бутылка воды",
    "на полу коврик",
    "на фоне растения",
    "полотенце на плече",
    "кроссовки в кадре",
    "часы на запястье",
    "дневник и ручка рядом",
    "солнечные блики на стене",
]

ANGLES: List[str] = [
    "крупный план, акцент на движении",
    "средний план, всё тело в кадре",
    "вид сбоку, профиль, динамичная поза",
    "ракурс чуть снизу, подчёркнутая устойчивость",
    "вид сверху, геометрия пространства",
    "кадр через плечо, взгляд в зеркало",
    "фрагмент: кисти рук и гриф, детали хвата",
    "съёмка с уровня пола, передний план размыт",
    "широкий кадр с пространством вокруг",
    "портрет по пояс, спокойный взгляд в камеру",
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
    "репортажная съёмка, лёгкое зерно",
    "мягкий плёночный оттенок, 85 мм",
]

NO_TEXT = "без текста, без надписей, без логотипов, без мультяшности"

# Проверка результата генерации: битый файл, слишком маленькая картинка,
# однотонная заглушка — такую в канал не публикуем.
IMAGE_VALIDATE = (os.getenv("IMAGE_VALIDATE", "true") or "").strip().lower() in {
    "1", "true", "yes", "y", "on",
}
IMAGE_MIN_SIDE = int(os.getenv("IMAGE_MIN_SIDE", "512"))
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "4096"))
IMAGE_MIN_BYTES = int(os.getenv("IMAGE_MIN_BYTES", "10000"))
IMAGE_MIN_STD = float(os.getenv("IMAGE_MIN_STD", "5"))

# Смысловая проверка картинки моделью: искажения, текст, несоответствие теме
IMAGE_VISION = (os.getenv("IMAGE_VISION", "true") or "").strip().lower() in {
    "1", "true", "yes", "y", "on",
}
VISION_MODEL = os.getenv("GIGACHAT_VISION_MODEL", "GigaChat-2-Max")
VISION_TIMEOUT = float(os.getenv("GIGACHAT_VISION_TIMEOUT", "60"))


def validate_image(data: bytes) -> Tuple[bool, str]:
    """Возвращает (годится ли картинка, причина отказа)."""
    if Image is None or ImageStat is None:
        return True, "Pillow не установлен — проверка пропущена"
    if not data:
        return False, "пустой файл"
    if len(data) < IMAGE_MIN_BYTES:
        return False, f"слишком маленький файл ({len(data)} байт)"
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 — битый файл от API
        return False, f"не читается как изображение: {exc}"

    width, height = image.size
    if min(width, height) < IMAGE_MIN_SIDE:
        return False, f"маленькое разрешение {width}×{height}"
    if max(width, height) > IMAGE_MAX_SIDE:
        return False, f"слишком большое разрешение {width}×{height}"

    gray = image.convert("L").resize((64, 64))
    deviation = ImageStat.Stat(gray).stddev[0]
    if deviation < IMAGE_MIN_STD:
        return False, f"однотонная картинка (разброс {deviation:.1f})"
    return True, ""

MAX_PROMPT_CHARS = 400   # длиннее Kandinsky начинает терять детали
MAX_BASE_CHARS = 200

def log_build_info() -> None:
    """Маркер сборки.

    Вызывать ТОЛЬКО после logging.basicConfig(): на импорте модуля обработчиков
    ещё нет, и INFO-сообщение молча теряется.
    """
    key_fp = hashlib.sha256(AUTH_KEY.encode()).hexdigest()[:8] if AUTH_KEY else "нет ключа"
    logger.info(
        "image_gen: модель=%s, таймаут=%s c, ретраи=%s, ключ=%s, вариации промта включены",
        MODEL, TIMEOUT, RETRIES, key_fp,
    )

_last_combos: List[Tuple[str, ...]] = []


def _trim_scene(scene: str, limit: int) -> str:
    """Обрезает сцену по границе слова: промт не должен кончаться на «сосредо»."""
    if len(scene) <= limit:
        return scene
    cut = scene[:limit]
    boundary = max(cut.rfind(" "), cut.rfind(","), cut.rfind(";"), cut.rfind("."))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:.-")


def build_prompt(base: str, category: str | None = None) -> str:
    """Собирает промт: сцена от модели + место по направлению + вариации съёмки.

    Место берётся из категории поста: растяжка — коврик, питание — кухня,
    сигналы тела — дом у зеркала. Так картинка хотя бы соответствует теме,
    а не показывает спортзал всегда.
    """
    scene = (base or "").strip().rstrip(" .;,") or FALLBACK_PROMPT
    scene = _trim_scene(scene, MAX_BASE_CHARS)

    scene_parts: List[str] = []
    if category and category in CATEGORY_SCENES:
        scene_parts.append(random.choice(CATEGORY_SCENES[category]))
        scene_parts.append(random.choice(DETAILS))

    combo: Tuple[str, ...] = ()
    for _ in range(8):
        parts = [
            random.choice(ANGLES),
            random.choice(LIGHTS),
            random.choice(PALETTES),
            random.choice(FORMATS),
        ]
        combo = tuple(parts)
        if combo not in _last_combos:
            break
    _last_combos.append(combo)
    while len(_last_combos) > 3:
        _last_combos.pop(0)

    prompt = ", ".join([scene, *scene_parts, *combo, NO_TEXT])
    if len(prompt) > MAX_PROMPT_CHARS:
        # режем сцену, а не модификаторы: они и дают разнообразие
        overflow = len(prompt) - MAX_PROMPT_CHARS
        scene = _trim_scene(scene, max(60, len(scene) - overflow))
        prompt = ", ".join([scene.rstrip(" ,"), *scene_parts, *combo, NO_TEXT])
    return prompt


VISION_PROMPT = (
    "Ты проверяешь изображение для поста в канале о спорте и питании.\n"
    "Тема поста: {topic}\n\n"
    "Ответь строго JSON без пояснений: {{\"ok\": true, \"reason\": \"\"}} "
    "или {{\"ok\": false, \"reason\": \"коротко почему\"}}.\n"
    "Плохо, если: искажённые лица, руки, пальцы или тело; нечитаемый или битый текст; "
    "бессмысленные артефакты; пугающие или медицинские сцены; картинка не соответствует теме. "
    "Если не уверен — считай, что ok: true."
)


def _vision_check_sync(data: bytes, topic: str) -> Tuple[bool, str]:
    with GigaChat(
        credentials=AUTH_KEY,
        model=VISION_MODEL,
        scope=SCOPE,
        timeout=VISION_TIMEOUT,
        max_retries=0,
        verify_ssl_certs=False,
    ) as gc:
        uploaded = gc.upload_file(io.BytesIO(data), purpose="general")
        file_id = getattr(uploaded, "id_", None) or getattr(uploaded, "id", "")
        response = gc.chat({
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": VISION_PROMPT.format(topic=(topic or "спорт и питание")[:200]),
                "attachments": [file_id],
            }],
            "temperature": 0.1,
        })
        answer = (response.choices[0].message.content or "").strip()

    start, end = answer.find("{"), answer.rfind("}")
    if start >= 0 and end > start:
        try:
            verdict = json.loads(answer[start:end + 1])
            return bool(verdict.get("ok", True)), str(verdict.get("reason", ""))[:200]
        except ValueError:
            pass
    lowered = answer.lower()
    if '"ok": false' in lowered or "'ok': false" in lowered:
        return False, answer[:200]
    return True, ""


async def check_image_semantics(data: bytes, topic: str = "") -> Tuple[bool, str]:
    """Проверка картинки моделью. Любая ошибка проверки не блокирует публикацию."""
    if not IMAGE_VISION or not AUTH_KEY:
        return True, ""
    try:
        return await asyncio.to_thread(_vision_check_sync, data, topic)
    except Exception as exc:  # noqa: BLE001 — сеть, модель без vision, лимиты
        logger.warning("Смысловая проверка картинки недоступна: %s", exc)
        return True, ""


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


async def fetch_image(prompt: str, settings, category: str | None = None,
                      topic: str = "") -> Optional[bytes]:
    """Возвращает байты картинки или None — пост уйдёт и без изображения."""
    text = build_prompt(prompt, category)
    if not AUTH_KEY:
        logger.error("Не задан GIGACHAT_AUTH_KEY в .env")
        return None
    logger.info("Промт картинки: %s", text)
    fallback: Optional[bytes] = None

    for attempt in (1, 2):
        current = text if attempt == 1 else (text + ", чёткая композиция, без артефактов")[:MAX_PROMPT_CHARS]
        try:
            data = await asyncio.to_thread(_generate_sync, current)
        except Exception as exc:  # noqa: BLE001 — сеть, API, формат ответа
            logger.warning("Картинка не получена (попытка %s): %s", attempt, exc)
            continue

        logger.info("Картинка получена: %s КБ (попытка %s)", len(data) // 1024, attempt)

        if IMAGE_VALIDATE:
            ok, reason = validate_image(data)
            if not ok:
                logger.warning("Техническая проверка не прошла: %s", reason)
                continue

        # Картинка технически годная: запоминаем как запасной вариант
        fallback = data

        ok, reason = await check_image_semantics(data, topic)
        if not ok:
            logger.warning("Смысловая проверка не прошла: %s", reason)
            continue

        return data

    if fallback is not None:
        logger.warning("Проверки не пропустили картинку, но публикую её как запасной вариант")
        return fallback
    logger.error("Картинку получить не удалось — публикую без неё")
    return None
