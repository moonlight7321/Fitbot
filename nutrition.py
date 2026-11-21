"""Рацион на неделю или месяц из базы продуктов.

Считается детерминированно: одинаковые анкета и дата дают одинаковый рацион.
Никакой модели — только продукты из foods.json и целевые калории/БЖУ.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Dict, List

import calories
import diary

PROTEINS = ["Куриная грудь", "Куриное бедро", "Индейка", "Говядина", "Треска",
            "Лосось", "Минтай", "Горбуша", "Тунец консервированный", "Креветки",
            "Яйцо куриное", "Творог 5%", "Творог 0%", "Скумбрия"]
CARBS = ["Гречка варёная", "Рис варёный", "Овсянка на воде", "Макароны варёные",
         "Картофель варёный", "Батат", "Булгур варёный", "Киноа варёная",
         "Перловка варёная", "Хлеб цельнозерновой"]
FRUITS = ["Банан", "Яблоко", "Апельсин", "Груша", "Киви", "Мандарин", "Виноград"]
DAIRY = ["Кефир 1%", "Йогурт натуральный", "Ряженка", "Молоко 2.5%", "Творог 0%"]
FATS = ["Авокадо", "Миндаль", "Грецкий орех", "Арахис"]

MEALS = [
    ("Завтрак", 0.25, "breakfast"),
    ("Обед", 0.35, "lunch"),
    ("Ужин", 0.30, "dinner"),
    ("Перекус", 0.10, "snack"),
]


def _food(name: str):
    return diary.find_food(name)


def _portion(food, grams: float) -> Dict:
    grams = max(5, round(grams / 5) * 5)
    factor = grams / 100.0
    return {
        "name": food.name,
        "grams": grams,
        "kcal": round(food.kcal * factor),
        "protein": round(food.protein * factor, 1),
        "fat": round(food.fat * factor, 1),
        "carbs": round(food.carbs * factor, 1),
    }


def _grams_for(food, nutrient_need: float, per_100: float, low: float, high: float) -> float:
    if per_100 <= 0:
        return low
    return max(low, min(high, nutrient_need / per_100 * 100))


def _grams_by_kcal(food, kcal_budget: float, low: float, high: float) -> float:
    if food.kcal <= 0:
        return low
    return max(low, min(high, kcal_budget / food.kcal * 100))


def _meal_items(rng: random.Random, kind: str, share: float, targets: Dict[str, float]) -> List[Dict]:
    """Порции считаются от калорийного бюджета приёма, а не «на глаз»."""
    items: List[Dict] = []
    budget = targets["kcal"] * share

    if kind == "snack":
        fruit = _food(rng.choice(FRUITS))
        dairy = _food(rng.choice(DAIRY))
        if fruit:
            items.append(_portion(fruit, _grams_by_kcal(fruit, budget * 0.55, 80, 250)))
        if dairy:
            items.append(_portion(dairy, _grams_by_kcal(dairy, budget * 0.45, 100, 300)))
        return items

    # Белок — примерно 30 % калорий приёма, углеводы — 45 %, жир — 12 %
    protein = _food(rng.choice(PROTEINS))
    if protein:
        items.append(_portion(protein, _grams_by_kcal(protein, budget * 0.30, 70, 300)))

    carb = _food(rng.choice(CARBS))
    if carb:
        items.append(_portion(carb, _grams_by_kcal(carb, budget * 0.45, 40, 400)))

    fat = _food(rng.choice(FATS))
    if fat and kind in ("breakfast", "dinner"):
        items.append(_portion(fat, _grams_by_kcal(fat, budget * 0.12, 5, 40)))

    if kind in ("lunch", "dinner"):
        veg = _food("Овощи (микс)")
        if veg:
            items.append(_portion(veg, 150 if kind == "lunch" else 200))

    if kind == "breakfast":
        fruit = _food(rng.choice(FRUITS))
        if fruit:
            items.append(_portion(fruit, _grams_by_kcal(fruit, budget * 0.10, 50, 200)))

    return items


def build(profile, days: int = 7, start: date | None = None) -> List[Dict]:
    result = calories.compute(profile)
    targets = {
        "kcal": float(result.target),
        "protein": float(result.protein_g),
        "fat": float(result.fat_g),
        "carbs": float(result.carbs_g),
    }
    start = start or date.today()
    days = max(1, min(31, days))

    plan: List[Dict] = []
    for index in range(days):
        day = start + timedelta(days=index)
        rng = random.Random(f"{profile.user_id}-{day.isoformat()}")
        meals = []
        total = {"kcal": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
        for title, share, kind in MEALS:
            items = _meal_items(rng, kind, share, targets)
            meal_kcal = sum(item["kcal"] for item in items)
            for item in items:
                total["kcal"] += item["kcal"]
                total["protein"] += item["protein"]
                total["fat"] += item["fat"]
                total["carbs"] += item["carbs"]
            meals.append({"title": title, "kcal": meal_kcal, "items": items})

        # Если до цели не хватает калорий — добираем хлебом в обед
        bread = _food("Хлеб цельнозерновой")
        deficit = targets["kcal"] - total["kcal"]
        if bread and deficit > 80:
            grams = min(220, max(20, deficit / bread.kcal * 100))
            extra = _portion(bread, grams)
            meals[1]["items"].append(extra)
            meals[1]["kcal"] += extra["kcal"]
            total["kcal"] += extra["kcal"]
            total["protein"] += extra["protein"]
            total["fat"] += extra["fat"]
            total["carbs"] += extra["carbs"]

        plan.append({
            "date": day.isoformat(),
            "weekday": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][day.weekday()],
            "meals": meals,
            "kcal": total["kcal"],
            "protein": round(total["protein"], 1),
            "fat": round(total["fat"], 1),
            "carbs": round(total["carbs"], 1),
            "target": targets["kcal"],
        })
    return plan
