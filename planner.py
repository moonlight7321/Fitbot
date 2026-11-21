"""Построение недельного плана из библиотеки упражнений.

Правила детерминированные: одинаковые ответы анкеты дают одинаковый план.
Модель здесь не участвует — она бы придумала несуществующие упражнения.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import exercises as lib
import profiles as prof


@dataclass
class Slot:
    exercise: "lib.Exercise"
    sets: int
    reps: str
    rest: int


@dataclass
class Day:
    title: str
    slots: List[Slot] = field(default_factory=list)


@dataclass
class Plan:
    days: List[Day]
    note: str


EXPERIENCE_LEVELS = {
    "novice": {"новичок"},
    "middle": {"новичок", "средний"},
    "advanced": {"новичок", "средний", "продвинутый"},
}

EQUIPMENT_ALLOWED = {
    "gym": None,  # всё
    "home": {"свой вес", "гантели", "гиря или гантель", "скакалка", "турник", "брусья"},
    "body": {"свой вес", "турник", "брусья", "скакалка"},
}

GOAL_PARAMS = {
    "mass": {"sets": 4, "reps": "8–10", "rest": 90},
    "relief": {"sets": 3, "reps": "12–15", "rest": 60},
    "tone": {"sets": 3, "reps": "12–15", "rest": 60},
}

# С собственным весом прогресс идёт через повторения, а не килограммы:
# опытному человеку 8–10 приседаний — это разминка, а не рабочий подход.
BODYWEIGHT_EQUIPMENT = {"свой вес", "турник", "брусья"}
BODYWEIGHT_REPS = {
    "Ноги": {"новичок": "10–15", "средний": "15–25", "продвинутый": "25–40"},
    "Спина": {"новичок": "6–10", "средний": "10–15", "продвинутый": "12–20"},
    "Грудь": {"новичок": "8–12", "средний": "12–20", "продвинутый": "20–30"},
    "Плечи": {"новичок": "8–12", "средний": "12–18", "продвинутый": "15–25"},
    "Руки": {"новичок": "8–12", "средний": "12–18", "продвинутый": "15–25"},
    "Пресс": {"новичок": "8–12", "средний": "12–20", "продвинутый": "20–30"},
}
DEFAULT_BODYWEIGHT_REPS = {"новичок": "8–12", "средний": "12–18", "продвинутый": "18–25"}


def reps_for(exercise: "lib.Exercise", profile: "prof.Profile", fallback: str) -> str:
    """Повторения под упражнение: для своего веса — по опыту и группе мышц."""
    if exercise.equipment not in BODYWEIGHT_EQUIPMENT:
        return fallback
    by_level = BODYWEIGHT_REPS.get(exercise.group, DEFAULT_BODYWEIGHT_REPS)
    return by_level.get(profile.experience, fallback)


def _top_reps(reps: str) -> int:
    """Верхняя граница диапазона: «8–10» → 10, «12–15» → 15, «10» → 10."""
    digits = [int(part) for part in reps.replace("–", "-").replace("—", "-").split("-") if part.strip().isdigit()]
    return max(digits) if digits else 10


def rep_scheme(goal: str, sets: int, top: int, timed: bool = False) -> List[int]:
    """Повторения по подходам с учётом усталости: 10, 9, 8, 8.

    Для удержаний (планка, боковая планка, «супермен») считаем секунды:
    45, 40, 35, 35 — иначе 10–15 «повторений» в планке бессмысленны.
    """
    if timed:
        # В плане для удержаний стоит диапазон повторений — он не про секунды.
        # Берём 45 секунд, но если в истории уже больше, стартуем от неё.
        start = top if top >= 30 else 45
        return [max(20, start - index * 5) for index in range(max(1, sets))]

    floor = 8 if goal == "mass" else 12
    top = max(top, floor)
    # На высоких повторениях шаг больше: 40 → 32 → 24, а не 40 → 39 → 38
    step = max(1, round(top * 0.2)) if top >= 20 else 1
    floor = max(floor, round(top * 0.5))
    return [max(floor, top - index * step) for index in range(max(1, sets))]


def sets_phrase(sets: int, reps: str, goal: str = "mass", timed: bool = False) -> str:
    """«4 подхода: 10 · 9 · 8 · 8» — видно, что нагрузка падает по подходам."""
    if sets % 100 in (11, 12, 13, 14):
        word = "подходов"
    elif sets % 10 == 1:
        word = "подход"
    elif sets % 10 in (2, 3, 4):
        word = "подхода"
    else:
        word = "подходов"

    scheme = rep_scheme(goal, sets, _top_reps(reps), timed)
    unit = " сек" if timed else ""
    return f"{sets} {word}: " + " · ".join(str(value) for value in scheme) + unit

SPLITS = {
    2: [("Всё тело, день A", ["Ноги", "Грудь", "Спина", "Пресс"]),
        ("Всё тело, день B", ["Ноги", "Спина", "Плечи", "Пресс"])],
    3: [("Всё тело, день A", ["Ноги", "Грудь", "Спина", "Пресс"]),
        ("Всё тело, день B", ["Ноги", "Спина", "Плечи", "Пресс"]),
        ("Всё тело, день C", ["Ноги", "Грудь", "Руки", "Пресс"])],
    4: [("Верх тела A", ["Грудь", "Спина", "Плечи", "Руки", "Пресс"]),
        ("Низ тела A", ["Ноги", "Ноги", "Пресс"]),
        ("Верх тела B", ["Спина", "Грудь", "Руки", "Плечи"]),
        ("Низ тела B", ["Ноги", "Ноги", "Пресс"])],
    5: [("Жимовой день", ["Грудь", "Плечи", "Руки", "Пресс"]),
        ("Тяговый день", ["Спина", "Спина", "Руки", "Пресс"]),
        ("Ноги", ["Ноги", "Ноги", "Пресс"]),
        ("Верх тела", ["Грудь", "Спина", "Плечи"]),
        ("Всё тело", ["Ноги", "Спина", "Грудь", "Пресс"])],
}

EXERCISES_BY_MINUTES = {20: 4, 30: 5, 45: 6, 60: 7}

# Что исключаем при ограничениях. Списки по slug — так проще проверять и
# дополнять, чем угадывать по названию.
EXCLUDE_BY_LIMIT = {
    "knees": {
        "Barbell_Squat", "Front_Barbell_Squat", "Hack_Squat", "Leg_Press",
        "Dumbbell_Lunges", "Box_Squat", "Goblet_Squat", "Bodyweight_Squat",
        "Rope_Jumping",
    },
    "back": {
        "Barbell_Deadlift", "Romanian_Deadlift", "Rack_Pulls", "Good_Morning",
        "Bent_Over_Barbell_Row", "T-Bar_Row_with_Handle", "Superman",
    },
    "shoulders": {
        "Barbell_Shoulder_Press", "Dumbbell_Shoulder_Press", "Arnold_Dumbbell_Press",
        "Front_Dumbbell_Raise", "Side_Lateral_Raise", "Upright_Barbell_Row",
        "Dips_-_Triceps_Version", "Incline_Dumbbell_Press",
    },
    "wrists": {
        "Pushups", "Plank", "Side_Bridge", "Mountain_Climbers", "Spider_Crawl",
        "Barbell_Curl", "Preacher_Curl", "Close-Grip_Barbell_Bench_Press",
    },
}


def excluded_slugs(limits: set) -> set:
    result: set = set()
    for code in limits:
        result |= EXCLUDE_BY_LIMIT.get(code, set())
    return result


def _allowed(ex: "lib.Exercise", experience: str, equipment: str, excluded: set) -> bool:
    if ex.slug in excluded:
        return False
    if ex.level not in EXPERIENCE_LEVELS.get(experience, {"новичок"}):
        return False
    allowed_equipment = EQUIPMENT_ALLOWED.get(equipment)
    if allowed_equipment is None:
        return True
    return ex.equipment in allowed_equipment


def _pick(group: str, pool: Dict[str, List["lib.Exercise"]], offset: int, used: set) -> "lib.Exercise | None":
    """Берёт упражнение группы, которого ещё не было в этом дне.

    Если группа исчерпана — возвращает None: лучше сделать меньше упражнений,
    чем два раза подряд одно и то же.
    """
    candidates = pool.get(group, [])
    if not candidates:
        return None
    for step in range(len(candidates)):
        ex = candidates[(offset + step) % len(candidates)]
        if ex.slug not in used:
            return ex
    return None


def build(profile: "prof.Profile", replacements: dict | None = None) -> Plan:
    excluded = excluded_slugs(profile.limits)
    pool: Dict[str, List["lib.Exercise"]] = {}
    for ex in lib.all_exercises():
        if _allowed(ex, profile.experience, profile.equipment, excluded):
            pool.setdefault(ex.group, []).append(ex)

    if not pool:
        return Plan(days=[], note="Под выбранные условия упражнений не нашлось — попробуйте другой инвентарь или уровень.")

    params = GOAL_PARAMS.get(profile.goal, GOAL_PARAMS["tone"])
    per_day = EXERCISES_BY_MINUTES.get(profile.minutes, 5)
    split = SPLITS.get(profile.days, SPLITS[3])

    days: List[Day] = []
    for day_index, (title, groups) in enumerate(split):
        day = Day(title=title)
        used: set = set()
        attempts = 0
        step = 0
        while len(day.slots) < per_day and attempts < per_day * 5:
            group = groups[step % len(groups)]
            step += 1
            attempts += 1
            ex = _pick(group, pool, day_index + step, used)
            if ex is None:
                continue
            used.add(ex.slug)
            day.slots.append(
                Slot(
                    exercise=ex,
                    sets=params["sets"],
                    reps=reps_for(ex, profile, params["reps"]),
                    rest=params["rest"],
                )
            )
        days.append(day)

    # Сохранённые замены: человек заменил упражнение — предлагаем замену и дальше
    replacements = replacements or getattr(profile, "replacements", None)
    if replacements:
        per_day = any(isinstance(key, int) for key in replacements)
        for day_index, day in enumerate(days):
            day_map = replacements.get(day_index, {}) if per_day else replacements
            if not day_map:
                continue
            for slot in day.slots:
                new_slug = day_map.get(slot.exercise.slug)
                if not new_slug:
                    continue
                replacement = lib.by_slug(new_slug)
                if replacement is not None:
                    slot.exercise = replacement

    note = (
        "Разминка 5–10 минут перед каждым днём, заминка — 3–5 минут. "
        "Если упражнение вызывает боль — не выполняйте его: замените на более простое "
        "или обратитесь к врачу/тренеру."
    )
    if "heart" in profile.limits:
        note += (
            " ⚠️ При сердечно-сосудистых заболеваниях или повышенном давлении "
            "нагрузку должен согласовать врач: этот план — общая информация, не назначение."
        )
    if excluded:
        note += " Упражнения с нагрузкой на проблемную зону исключены."

    note += (
        " Веса со знаком «≈» — стартовый ориентир. Если окажется тяжело или, "
        "наоборот, легко — поменяйте вес на следующей тренировке: бот запомнит "
        "и будет считать прогрессию от него."
    )
    if profile.goal == "mass":
        note += " Вес подбирайте так, чтобы последние 2 повтора давались тяжело, но без потери техники."
    elif profile.goal == "relief":
        note += " Отдых короче, но техника важнее темпа."
    return Plan(days=days, note=note)


def start_weight(body_weight: float, exercise) -> float:
    """Ориентир стартового веса: процент от массы тела по снаряду и группе."""
    equipment = (exercise.equipment or "").lower()
    group = exercise.group

    if "свой вес" in equipment or body_weight <= 0:
        return 0.0

    if "гантел" in equipment:
        pct = 0.10 if group == "Ноги" else 0.07
        step = 1.0
    elif "штанг" in equipment:
        pct = 0.20 if group == "Ноги" else 0.12
        step = 2.5
    else:
        pct = 0.20
        step = 2.5

    raw = body_weight * pct
    return max(step, round(raw / step) * step)


def format_weight(weight: float, equipment: str) -> str:
    """Гантели — вес одной гантели, остальное — общий вес снаряда."""
    if not weight or weight <= 0:
        return "вес тела"
    if "гантел" in (equipment or "").lower():
        return f"по {weight:g} кг"
    return f"{weight:g} кг"


def render(profile: "prof.Profile", plan: Plan, weights: dict | None = None,
           deload: bool = False, weak_groups: set | None = None,
           pain_groups: set | None = None, only_day: int | None = None) -> str:
    """weights: slug -> {"label": "по 10 кг", "reps": 10 или None}."""

    lines = [
        "<b>Ваш план</b>" if only_day is not None else "<b>Ваш план на неделю</b>",
        f"{profile.goal_title} · {profile.experience_title} · {profile.equipment_title}",
        f"{profile.days} тренировки(и) в неделю по {profile.minutes} минут",
        f"Ограничения: {profile.limitations_title}",
    ]
    if deload:
        lines.append(
            "🟢 <b>Разгрузочная неделя</b>: подходов меньше, вес тот же — "
            "так уходит накопленная усталость."
        )
    lines.append("")
    days = list(enumerate(plan.days, 1))
    if only_day is not None and 0 <= only_day < len(plan.days):
        days = [(only_day + 1, plan.days[only_day])]

    for index, day in days:
        if only_day is not None:
            lines.append(f"<b>День {index} из {len(plan.days)} · {day.title}</b>")
        else:
            lines.append(f"<b>День {index} · {day.title}</b>")
        if not day.slots:
            lines.append("— упражнений не нашлось")
        for n, slot in enumerate(day.slots, 1):
            sets_text = max(2, slot.sets // 2) if deload else slot.sets
            if pain_groups and slot.exercise.group in pain_groups:
                sets_text = max(2, sets_text - 1)
            suffix = ""
            reps_text = slot.reps
            target = (weights or {}).get(slot.exercise.slug)
            if target:
                if target.get("reps"):
                    reps_text = str(target["reps"])
                if target.get("label"):
                    suffix = f" · <b>{target['label']}</b>"
            if pain_groups and slot.exercise.group in pain_groups:
                mark = "🩹 "
            elif weak_groups and slot.exercise.group in weak_groups:
                mark = "⚠️ "
            else:
                mark = ""
            lines.append(
                f"{n}. {mark}{slot.exercise.name_ru} — "
                f"{sets_phrase(sets_text, reps_text, profile.goal, slot.exercise.timed)}{suffix}"
            )
        lines.append("")
    lines.append(f"<i>{plan.note}</i>")
    return "\n".join(lines)
