"""Прогресс: журнал выполненных тренировок и правило следующей недели.

Числа считает код: модель не должна придумывать веса.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    day_index  INTEGER NOT NULL,
    day_title  TEXT NOT NULL,
    done_on    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workout_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id INTEGER NOT NULL,
    slug       TEXT NOT NULL,
    set_no     INTEGER NOT NULL,
    weight     REAL NOT NULL,
    reps       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workouts_user ON workouts (user_id, done_on);
CREATE INDEX IF NOT EXISTS idx_sets_workout ON workout_sets (workout_id);
"""

SET_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+)", re.IGNORECASE)


@dataclass
class SetRecord:
    slug: str
    weight: float
    reps: int


def _today() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def parse_line(line: str) -> Optional[Tuple[str, List[Tuple[float, int]]]]:
    """«приседания 60x8, 60x8, 60x7» -> ('приседания', [(60,8),(60,8),(60,7)])."""
    text = (line or "").strip()
    if not text:
        return None
    first = SET_RE.search(text)
    if first is None:
        return None
    name = text[: first.start()].strip(" -–—:,")
    if not name:
        return None
    sets = [
        (float(weight.replace(",", ".")), int(reps))
        for weight, reps in SET_RE.findall(text)
    ]
    return name, sets


def _add_sync(user_id: int, day_index: int, day_title: str,
              records: Sequence[SetRecord]) -> int:
    conn = _open()
    try:
        now = datetime.now(TIMEZONE)
        cur = conn.execute(
            "INSERT INTO workouts (user_id, day_index, day_title, done_on, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, day_index, day_title, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        workout_id = int(cur.lastrowid)
        counters: Dict[str, int] = {}
        for record in records:
            counters[record.slug] = counters.get(record.slug, 0) + 1
            conn.execute(
                "INSERT INTO workout_sets (workout_id, slug, set_no, weight, reps) VALUES (?, ?, ?, ?, ?)",
                (workout_id, record.slug, counters[record.slug], record.weight, record.reps),
            )
        conn.commit()
        return workout_id
    finally:
        conn.close()


def _last_sets_sync(user_id: int, slug: str) -> List[sqlite3.Row]:
    conn = _open()
    try:
        row = conn.execute(
            """SELECT ws.* FROM workout_sets ws
               JOIN workouts w ON w.id = ws.workout_id
               WHERE w.user_id = ? AND ws.slug = ?
               ORDER BY ws.workout_id DESC, ws.set_no ASC LIMIT 20""",
            (user_id, slug),
        ).fetchall()
        if not row:
            return []
        last_workout = row[0]["workout_id"]
        return [r for r in row if r["workout_id"] == last_workout]
    finally:
        conn.close()


def _history_sync(user_id: int, days: int) -> List[sqlite3.Row]:
    since = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _open()
    try:
        return conn.execute(
            """SELECT w.done_on, ws.slug, MAX(ws.weight) AS weight, MAX(ws.reps) AS reps
               FROM workout_sets ws JOIN workouts w ON w.id = ws.workout_id
               WHERE w.user_id = ? AND w.done_on >= ?
               GROUP BY w.done_on, ws.slug ORDER BY w.done_on ASC""",
            (user_id, since),
        ).fetchall()
    finally:
        conn.close()


def _count_sync(user_id: int) -> int:
    conn = _open()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM workouts WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["n"])
    finally:
        conn.close()


async def add_workout(user_id: int, day_index: int, day_title: str,
                      records: Sequence[SetRecord]) -> int:
    return await asyncio.to_thread(_add_sync, user_id, day_index, day_title, records)


async def last_sets(user_id: int, slug: str):
    return await asyncio.to_thread(_last_sets_sync, user_id, slug)


async def history(user_id: int, days: int = 28):
    return await asyncio.to_thread(_history_sync, user_id, days)


async def count(user_id: int) -> int:
    return await asyncio.to_thread(_count_sync, user_id)


def _top_reps(goal: str) -> int:
    import planner
    reps = planner.GOAL_PARAMS.get(goal, planner.GOAL_PARAMS["tone"])["reps"]
    try:
        return int(reps.split("–")[-1])
    except ValueError:
        return 12


def _step_for(equipment: str) -> float:
    eq = (equipment or "").lower()
    if "штанг" in eq:
        return 2.5
    if "гантел" in eq:
        return 2.0
    return 2.5


def next_plan(goal: str, equipment: str, sets: Sequence[sqlite3.Row]) -> dict:
    """Вес и повторения на следующую тренировку (для подстановки в план)."""
    if not sets:
        return {"weight": 0.0, "reps": None, "bodyweight": False}

    top = _top_reps(goal)
    weight = float(sets[-1]["weight"])
    best = max(int(s["reps"]) for s in sets)
    bodyweight = weight <= 0 or "свой вес" in (equipment or "").lower()

    if bodyweight:
        return {"weight": 0.0, "reps": best + 1, "bodyweight": True}
    if best >= top:
        return {"weight": weight + _step_for(equipment), "reps": top, "bodyweight": False}
    return {"weight": weight, "reps": best + 1, "bodyweight": False}


def next_advice(goal: str, equipment: str, sets: Sequence[sqlite3.Row]) -> str:
    """Что делать на следующей тренировке с этим упражнением."""
    if not sets:
        return "первый раз — подберите вес так, чтобы оставалось 1–2 повторения в запасе"

    top = _top_reps(goal)
    weight = float(sets[-1]["weight"])
    best = max(int(s["reps"]) for s in sets)
    bodyweight = weight <= 0 or "свой вес" in (equipment or "").lower()

    if bodyweight:
        if best >= top:
            return f"{best + 1} повторений (вес тела)"
        return f"{best + 1} повторений, добавьте подход"

    if best >= top:
        step = _step_for(equipment)
        return f"{weight + step:g} кг ({weight:g} → {weight + step:g})"
    return f"{weight:g} кг, повторите {best + 1}+ повторений"
