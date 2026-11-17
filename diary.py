"""Дневник питания: продукты из foods.json, записи в SQLite по дням."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
FOODS_PATH = Path(os.getenv("FOODS_PATH", BASE_DIR / "foods.json"))
DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    day        TEXT NOT NULL,
    name       TEXT NOT NULL,
    grams      REAL NOT NULL,
    kcal       REAL NOT NULL,
    protein    REAL NOT NULL,
    fat        REAL NOT NULL,
    carbs      REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meals_user_day ON meals (user_id, day);
"""


@dataclass(frozen=True)
class Food:
    name: str
    kcal: float
    protein: float
    fat: float
    carbs: float


def _load_foods() -> List[Food]:
    if not FOODS_PATH.exists():
        logger.error("Нет файла продуктов: %s", FOODS_PATH)
        return []
    raw = json.loads(FOODS_PATH.read_text(encoding="utf-8"))
    return [
        Food(name=item["name"], kcal=float(item["kcal"]), protein=float(item["protein"]),
             fat=float(item["fat"]), carbs=float(item["carbs"]))
        for item in raw
    ]


FOODS: List[Food] = _load_foods()


def _norm(text: str) -> str:
    return (text or "").strip().lower().replace("ё", "е")


def _word_match(q: str, name: str) -> bool:
    return re.search(rf"(^|\W){re.escape(q)}(\W|$)", name) is not None


def find_food(query: str) -> Optional[Food]:
    """Ищет продукт: точное имя → целое слово → начало слова → основа → подстрока.

    Порядок важен: «сыр» должен находить «Сыр твёрдый», а не «Сырники»,
    а «курица» — «Куриную грудь», хотя точной подстроки там нет.
    """
    q = _norm(query)
    if not q:
        return None

    for food in FOODS:
        if _norm(food.name) == q:
            return food
    for food in FOODS:
        if _word_match(q, _norm(food.name)):
            return food
    for food in FOODS:
        if _norm(food.name).startswith(q):
            return food
    for food in FOODS:
        if q in _norm(food.name):
            return food
    # «куринные котлеты» vs «Котлеты куриные»: слова в другом порядке и с опечаткой
    tokens = [t for t in q.split() if len(t) > 3]
    if len(tokens) > 1:
        found = [
            food for food in FOODS
            if all(any(word[:5] == token[:5] for word in _norm(food.name).split())
                   for token in tokens)
        ]
        if found:
            return sorted(found, key=lambda food: len(food.name))[0]

    if len(q) >= 4:
        stem = q[:4]
        for food in FOODS:
            if _norm(food.name).startswith(stem):
                return food
    return None


def suggest(query: str, limit: int = 3) -> List[str]:
    q = _norm(query)
    if not q:
        return []
    stem = q[:4]
    return [f.name for f in FOODS if _norm(f.name).startswith(stem)][:limit]


def today() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def _add_sync(user_id: int, day: str, name: str, grams: float,
              kcal: float, protein: float, fat: float, carbs: float) -> None:
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO meals (user_id, day, name, grams, kcal, protein, fat, carbs, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, day, name, grams, kcal, protein, fat, carbs,
             datetime.now(TIMEZONE).strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


def _items_sync(user_id: int, day: str) -> List[Dict]:
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT * FROM meals WHERE user_id = ? AND day = ? ORDER BY id", (user_id, day)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _delete_last_sync(user_id: int, day: str) -> bool:
    conn = _open()
    try:
        row = conn.execute(
            "SELECT id FROM meals WHERE user_id = ? AND day = ? ORDER BY id DESC LIMIT 1",
            (user_id, day),
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM meals WHERE id = ?", (row["id"],))
        conn.commit()
        return True
    finally:
        conn.close()


def _clear_sync(user_id: int, day: str) -> int:
    conn = _open()
    try:
        cur = conn.execute("DELETE FROM meals WHERE user_id = ? AND day = ?", (user_id, day))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


async def add(user_id: int, food: Food, grams: float, day: Optional[str] = None) -> None:
    ratio = grams / 100.0
    await asyncio.to_thread(
        _add_sync, user_id, day or today(), food.name, grams,
        round(food.kcal * ratio, 1), round(food.protein * ratio, 1),
        round(food.fat * ratio, 1), round(food.carbs * ratio, 1),
    )


async def items(user_id: int, day: Optional[str] = None) -> List[Dict]:
    return await asyncio.to_thread(_items_sync, user_id, day or today())


async def delete_last(user_id: int, day: Optional[str] = None) -> bool:
    return await asyncio.to_thread(_delete_last_sync, user_id, day or today())


async def clear(user_id: int, day: Optional[str] = None) -> int:
    return await asyncio.to_thread(_clear_sync, user_id, day or today())


def _history_sync(user_id: int, days: int) -> List[Dict]:
    conn = _open()
    try:
        rows = conn.execute(
            """SELECT day, ROUND(SUM(kcal), 1) AS kcal,
                      ROUND(SUM(protein), 1) AS protein,
                      ROUND(SUM(fat), 1) AS fat,
                      ROUND(SUM(carbs), 1) AS carbs
               FROM meals WHERE user_id = ?
               GROUP BY day ORDER BY day DESC LIMIT ?""",
            (user_id, days),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


async def history(user_id: int, days: int = 14) -> List[Dict]:
    return await asyncio.to_thread(_history_sync, user_id, days)


def totals(rows: List[Dict]) -> Dict[str, float]:
    return {
        "kcal": round(sum(r["kcal"] for r in rows), 1),
        "protein": round(sum(r["protein"] for r in rows), 1),
        "fat": round(sum(r["fat"] for r in rows), 1),
        "carbs": round(sum(r["carbs"] for r in rows), 1),
    }
