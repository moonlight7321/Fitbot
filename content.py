"""Банк тем и форматов для автопостинга.

Темы разложены по направлениям: тренировки, растяжка и мобильность,
сигналы тела, питание, мифы, привычки и анатомия. Формат подбирается
по кругу, чтобы соседние посты не были однотипными.
"""
from __future__ import annotations

import random
from typing import Dict, List

FORMATS: List[str] = [
    "чек-лист из 5–7 коротких пунктов",
    "разбор одного популярного мифа",
    "инструкция по шагам: как сделать сегодня",
    "объяснение простыми словами: почему это работает",
    "3 сигнала и что с ними делать",
    "вопрос читателя и короткий ответ",
    "микро-совет на 1 минуту",
]

CATEGORY_HINTS: Dict[str, str] = {
    "тренировка": "Дай конкретное упражнение или короткую схему с подходами и повторениями, "
                  "укажи вариант для новичка и для опытного.",
    "растяжка": "Опиши, как тянуть: положение, дыхание, сколько держать, что должно "
                "чувствоваться и чего быть не должно. Без резких движений и боли.",
    "сигналы тела": "Пиши как наблюдатель, а не врач: что может значить сигнал, что сделать "
                    "в ближайшие дни и когда идти к врачу. Никаких диагнозов и лечения.",
    "питание": "Только общие принципы: порция, замена, режим. Без индивидуальных расчётов "
               "и запретов на целые группы продуктов.",
    "мифы": "Сначала сформулируй миф, потом коротко объясни, что не так и как на самом деле.",
    "привычки": "Предложи один конкретный микрошаг, который делается за 5 минут и не требует силы воли.",
    "анатомия": "Объясни механику простыми словами: что происходит в теле и почему это важно.",
}

# (тема, направление)
TOPICS: List[Dict[str, str]] = [
    # тренировки
    {"topic": "Короткая тренировка на 15–30 минут без оборудования", "category": "тренировка"},
    {"topic": "Полноценная тренировка на 45–60 минут: разминка, основная часть, заминка", "category": "тренировка"},
    {"topic": "Тренировка дома без инвентаря на всё тело", "category": "тренировка"},
    {"topic": "Как накачать предплечья и усилить хват", "category": "тренировка"},
    {"topic": "Тренировка на спину без турника", "category": "тренировка"},
    {"topic": "Как прокачать ягодицы без приседаний", "category": "тренировка"},
    {"topic": "Тренировка на пресс: 3 упражнения и частые ошибки", "category": "тренировка"},
    {"topic": "Как тренировать плечи без боли в суставе", "category": "тренировка"},
    {"topic": "Что делать, если нет времени на спортзал", "category": "тренировка"},
    {"topic": "Прогрессивная перегрузка: как расти без травм", "category": "тренировка"},

    # растяжка и мобильность
    {"topic": "Растяжка после тренировки: 5 минут, которые снимают забитость", "category": "растяжка"},
    {"topic": "Мобильность таза: зачем она нужна и 3 простых упражнения", "category": "растяжка"},
    {"topic": "Растяжка плеч и грудного отдела для сидячей работы", "category": "растяжка"},
    {"topic": "Динамическая разминка перед тренировкой вместо статической растяжки", "category": "растяжка"},
    {"topic": "Растяжка на работе: 3 движения за 2 минуты", "category": "растяжка"},
    {"topic": "Как тянуть заднюю поверхность бедра и не потянуть её", "category": "растяжка"},
    {"topic": "Растяжка для тех, кто много сидит: шея и поясница", "category": "растяжка"},

    # сигналы тела
    {"topic": "Сигналы тела на тренировке: когда остановиться", "category": "сигналы тела"},
    {"topic": "Боль в колене после приседа: что проверить в первую очередь", "category": "сигналы тела"},
    {"topic": "Крепатура или травма: как отличить", "category": "сигналы тела"},
    {"topic": "Онемение рук после тренировки: на что обратить внимание", "category": "сигналы тела"},
    {"topic": "Головокружение после подхода: причины и что делать", "category": "сигналы тела"},
    {"topic": "Постоянная усталость: тренировки, сон или питание?", "category": "сигналы тела"},
    {"topic": "Ноющая боль в пояснице после становой: чек-лист", "category": "сигналы тела"},
    {"topic": "Тянет в боку на кардио: почему и как дышать", "category": "сигналы тела"},

    # питание
    {"topic": "Сколько белка нужно на самом деле и где его взять", "category": "питание"},
    {"topic": "Как собрать сбалансированную тарелку без взвешивания", "category": "питание"},
    {"topic": "Перекусы, которые реально насыщают", "category": "питание"},
    {"topic": "Вода и тренировки: сколько пить и когда", "category": "питание"},
    {"topic": "Кофе перед тренировкой: польза, дозировка, ограничения", "category": "питание"},
    {"topic": "Клетчатка: зачем она нужна и где её взять", "category": "питание"},
    {"topic": "Как читать этикетки и не попадаться на маркетинг", "category": "питание"},

    # мифы
    {"topic": "Миф: локально сжечь жир на животе можно упражнениями", "category": "мифы"},
    {"topic": "Миф: кардио натощак сжигает больше жира", "category": "мифы"},
    {"topic": "Миф: растяжка до тренировки защищает от травм", "category": "мифы"},
    {"topic": "Миф: много повторений с лёгким весом — это тонус, а не масса", "category": "мифы"},
    {"topic": "Миф: без протеина мышцы не растут", "category": "мифы"},

    # привычки
    {"topic": "Микрошаг на сегодня: 10 минут движения, которые точно влезут в день", "category": "привычки"},
    {"topic": "Как не бросить тренировки после первой недели", "category": "привычки"},
    {"topic": "Планирование недели: 15 минут в воскресенье", "category": "привычки"},
    {"topic": "Что делать после срыва режима", "category": "привычки"},

    # анатомия
    {"topic": "Как растут мышцы: простыми словами о нагрузке и восстановлении", "category": "анатомия"},
    {"topic": "Зачем нужна разминка: что происходит с мышцами и суставами", "category": "анатомия"},
    {"topic": "Почему болит всё после первой тренировки и как это пережить", "category": "анатомия"},
    {"topic": "Что такое забитость и почему она проходит не сразу", "category": "анатомия"},
]


WINDOW = 4  # сколько последних постов помним, чтобы не повторяться


class Rotation:
    """Темы, направления и форматы не повторяются в пределах четырёх постов."""

    def __init__(self, topics: List[Dict[str, str]] | None = None) -> None:
        source = topics or TOPICS
        self._by_category: Dict[str, List[str]] = {}
        for item in source:
            self._by_category.setdefault(item["category"], []).append(item["topic"])

        self._categories = list(self._by_category.keys())
        self._cat_index = 0
        self._topic_index: Dict[str, int] = {cat: 0 for cat in self._categories}
        self._format_index = 0
        self._recent_categories: list = []
        self._recent_topics: list = []
        self._recent_formats: list = []

    def _remember(self, bucket: list, value: str) -> None:
        bucket.append(value)
        while len(bucket) > WINDOW:
            bucket.pop(0)

    def next_topic(self) -> Dict[str, str]:
        # направление: берём следующее по кругу, пропуская те, что были в окне
        category = self._categories[self._cat_index % len(self._categories)]
        for _ in range(len(self._categories)):
            candidate = self._categories[self._cat_index % len(self._categories)]
            self._cat_index += 1
            if candidate not in self._recent_categories:
                category = candidate
                break

        topics_list = self._by_category[category]
        topic = topics_list[self._topic_index[category] % len(topics_list)]
        for _ in range(len(topics_list)):
            candidate = topics_list[self._topic_index[category] % len(topics_list)]
            self._topic_index[category] += 1
            if candidate not in self._recent_topics:
                topic = candidate
                break

        fmt = FORMATS[self._format_index % len(FORMATS)]
        for _ in range(len(FORMATS)):
            candidate = FORMATS[self._format_index % len(FORMATS)]
            self._format_index += 1
            if candidate not in self._recent_formats:
                fmt = candidate
                break

        self._remember(self._recent_categories, category)
        self._remember(self._recent_topics, topic)
        self._remember(self._recent_formats, fmt)
        return {"topic": topic, "category": category, "format": fmt}

    def random_format(self) -> str:
        return random.choice(FORMATS)


def hint_for(item: Dict[str, str]) -> str:
    """Собирает инструкцию для модели: формат и правила направления."""
    parts = []
    if item.get("format"):
        parts.append(f"Формат поста: {item['format']}.")
    category = item.get("category", "")
    if category in CATEGORY_HINTS:
        parts.append(CATEGORY_HINTS[category])
    return " ".join(parts)
