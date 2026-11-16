"""Расчёт калорий и БЖУ по формуле Миффлина–Сан Жеора.

Считает код, а не модель: формула детерминирована и проверяема. Модель
пригодится только для текста, но числа она путает.

Это ориентир, а не медицинское назначение: точность ±10–15 %, пересчитывать
по динамике веса раз в 2–4 недели.
"""
from __future__ import annotations

from dataclasses import dataclass

import profiles as prof

ACTIVITY_BY_DAYS = {
    2: 1.375,
    3: 1.45,
    4: 1.55,
    5: 1.65,
}

GOAL_ADJUST = {
    "mass": 0.10,     # профицит 10 %
    "relief": -0.15,  # дефицит 15 %
    "tone": -0.05,    # мягкий дефицит 5 %
}

# Ниже этих значений опускаться нельзя даже при цели «рельеф»
ABS_FLOOR = {"m": 1500, "f": 1200}


@dataclass
class Result:
    bmr: int
    tdee: int
    target: int
    protein_g: int
    fat_g: int
    carbs_g: int
    floor_applied: bool


def _round_kcal(value: float, step: int = 10) -> int:
    return int(round(value / step) * step)


def _round_g(value: float) -> int:
    return int(round(value / 5) * 5)


def compute(profile: "prof.Profile") -> Result:
    weight = float(profile.weight)
    height = float(profile.height)
    age = float(profile.age)
    sex = "m" if profile.sex == "m" else "f"

    # Миффлин–Сан Жеор
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if sex == "m" else -161)

    factor = ACTIVITY_BY_DAYS.get(profile.days, 1.45)
    tdee = bmr * factor

    adjust = GOAL_ADJUST.get(profile.goal, GOAL_ADJUST["tone"])
    target = tdee * (1 + adjust)

    floor = max(bmr, ABS_FLOOR[sex])
    floor_applied = target < floor
    if floor_applied:
        target = floor

    protein_per_kg = 2.0 if profile.goal == "mass" else 1.8
    fat_per_kg = 0.9

    protein_g = _round_g(weight * protein_per_kg)
    fat_g = _round_g(weight * fat_per_kg)

    carbs_kcal = target - protein_g * 4 - fat_g * 9
    carbs_g = _round_g(max(carbs_kcal, 0) / 4)

    return Result(
        bmr=_round_kcal(bmr),
        tdee=_round_kcal(tdee),
        target=_round_kcal(target),
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=max(carbs_g, 0),
        floor_applied=floor_applied,
    )


def render(profile: "prof.Profile", result: Result) -> str:
    adjust = GOAL_ADJUST.get(profile.goal, GOAL_ADJUST["tone"])
    if adjust > 0:
        goal_note = f"+{int(adjust * 100)} % к расходу"
    elif adjust < 0:
        goal_note = f"{int(adjust * 100)} % к расходу"
    else:
        goal_note = "поддержание"

    lines = [
        "<b>Калории и БЖУ</b>",
        f"{profile.weight} кг · {profile.height} см · {profile.age} лет · "
        f"{'мужской' if profile.sex == 'm' else 'женский'}",
        f"Активность: {profile.days} тренировки(и) в неделю",
        "",
        f"Базовый обмен (BMR): <b>{result.bmr}</b> ккал",
        f"С учётом активности: <b>{result.tdee}</b> ккал",
        f"Цель «{profile.goal_title}»: <b>{result.target}</b> ккал ({goal_note})",
        "",
        f"Белки: <b>{result.protein_g} г</b>",
        f"Жиры: <b>{result.fat_g} г</b>",
        f"Углеводы: <b>{result.carbs_g} г</b>",
    ]

    if result.floor_applied:
        lines.append("")
        lines.append(
            "<i>Калории подняты до безопасного минимума: при вашей цели "
            "больший дефицит был бы слишком агрессивным.</i>"
        )

    lines.append("")
    lines.append(
        "<i>Это ориентир, а не назначение. Точность ±10–15 %, "
        "пересчитывать стоит раз в 2–4 недели по изменению веса.</i>"
    )
    return "\n".join(lines)
