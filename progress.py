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
CREATE TABLE IF NOT EXISTS failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    day_index  INTEGER NOT NULL,
    slug       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_user ON failures (user_id, week);
CREATE TABLE IF NOT EXISTS workout_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    day_index  INTEGER NOT NULL,
    done       INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, week, day_index)
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

        # Записанная тренировка = выполненная: отметка нужна для разгрузки
        week = _week_index_sync(user_id)
        conn.execute(
            """INSERT INTO workout_checks (user_id, week, day_index, done, created_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(user_id, week, day_index) DO UPDATE SET done = 1""",
            (user_id, week, day_index, now.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
        return workout_id
    finally:
        conn.close()


def _last_sets_sync(user_id: int, slug: str) -> List[sqlite3.Row]:
    anchor = _anchor_sync(user_id)
    since = anchor.strftime("%Y-%m-%d") if anchor else "0000-00-00"
    conn = _open()
    try:
        row = conn.execute(
            """SELECT ws.* FROM workout_sets ws
               JOIN workouts w ON w.id = ws.workout_id
               WHERE w.user_id = ? AND ws.slug = ? AND w.done_on >= ?
               ORDER BY ws.workout_id DESC, ws.set_no ASC LIMIT 20""",
            (user_id, slug, since),
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


def _first_date_sync(user_id: int):
    conn = _open()
    try:
        row = conn.execute(
            "SELECT MIN(done_on) AS first FROM workouts WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["first"] if row and row["first"] else None
    finally:
        conn.close()


GAP_DAYS = 14  # пропуск 2+ недель начинает новый цикл


def _anchor_sync(user_id: int):
    """Дата начала текущего цикла: после разрыва в 2+ недели отсчёт начинается заново."""
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT DISTINCT done_on FROM workouts WHERE user_id = ? ORDER BY done_on",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    dates = []
    for row in rows:
        try:
            dates.append(datetime.strptime(row["done_on"], "%Y-%m-%d").date())
        except (TypeError, ValueError):
            continue
    if not dates:
        return None

    anchor = dates[0]
    for prev, current in zip(dates, dates[1:]):
        if (current - prev).days >= GAP_DAYS:
            anchor = current

    today = datetime.now(TIMEZONE).date()
    if (today - dates[-1]).days >= GAP_DAYS:
        # давно не тренировался — старый цикл закрыт, начинаем с сегодня
        anchor = today
    return anchor


def _week_index_sync(user_id: int) -> int:
    anchor = _anchor_sync(user_id)
    if anchor is None:
        return 1
    today = datetime.now(TIMEZONE).date()
    return max(1, (today - anchor).days // 7 + 1)


def _week_bounds_sync(user_id: int):
    anchor = _anchor_sync(user_id)
    today = datetime.now(TIMEZONE).date()
    if anchor is None:
        return 1, today - timedelta(days=6)
    index = max(1, (today - anchor).days // 7 + 1)
    return index, anchor + timedelta(days=7 * (index - 1))


def _week_done_days_sync(user_id: int, start) -> set:
    start_str = start.strftime("%Y-%m-%d")
    end_str = (start + timedelta(days=7)).strftime("%Y-%m-%d")
    conn = _open()
    try:
        rows = conn.execute(
            """SELECT DISTINCT day_index FROM workouts
               WHERE user_id = ? AND done_on >= ? AND done_on < ?""",
            (user_id, start_str, end_str),
        ).fetchall()
        return {int(r["day_index"]) for r in rows}
    finally:
        conn.close()


def _add_failures_sync(user_id: int, week: int, day_index: int, items) -> None:
    conn = _open()
    try:
        now = datetime.now(TIMEZONE).strftime("%Y-%m-%dT%H:%M:%S")
        for slug, reason in items:
            conn.execute(
                """INSERT INTO failures (user_id, week, day_index, slug, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, week, day_index, slug, reason, now),
            )
        conn.commit()
    finally:
        conn.close()


def _failures_sync(user_id: int, days: int):
    since = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _open()
    try:
        return conn.execute(
            "SELECT slug, reason, week, day_index FROM failures "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, since),
        ).fetchall()
    finally:
        conn.close()


def _checks_sync(user_id: int, week: int):
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT day_index, done FROM workout_checks WHERE user_id = ? AND week = ?",
            (user_id, week),
        ).fetchall()
        return {int(r["day_index"]): int(r["done"]) for r in rows}
    finally:
        conn.close()


def _set_check_sync(user_id: int, week: int, day_index: int, done) -> None:
    # 0 — пропуск, 1 — выполнено, 2 — частично
    value = int(done)
    if value not in (0, 1, 2):
        value = 1 if value else 0
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO workout_checks (user_id, week, day_index, done, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, week, day_index) DO UPDATE SET done = excluded.done""",
            (user_id, week, day_index, value,
             datetime.now(TIMEZONE).strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


def _series_sync(user_id: int, slug: str, days: int):
    anchor = _anchor_sync(user_id)
    since = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
    if anchor is not None and anchor.strftime("%Y-%m-%d") > since:
        since = anchor.strftime("%Y-%m-%d")
    conn = _open()
    try:
        return conn.execute(
            """SELECT w.done_on, MAX(ws.weight) AS weight, MAX(ws.reps) AS reps
               FROM workout_sets ws JOIN workouts w ON w.id = ws.workout_id
               WHERE w.user_id = ? AND ws.slug = ? AND w.done_on >= ?
               GROUP BY w.done_on ORDER BY w.done_on ASC""",
            (user_id, slug, since),
        ).fetchall()
    finally:
        conn.close()


def _history_weeks_sync(user_id: int, limit: int) -> dict:
    anchor = _anchor_sync(user_id)
    if anchor is None:
        return {"weeks": [], "experience": 0, "total": 0, "streak": 0}

    start0 = anchor
    today = datetime.now(TIMEZONE).date()
    current = max(1, (today - start0).days // 7 + 1)

    conn = _open()
    try:
        workouts = conn.execute(
            "SELECT done_on, day_index FROM workouts WHERE user_id = ? AND done_on >= ?",
            (user_id, start0.strftime("%Y-%m-%d")),
        ).fetchall()
        checks = conn.execute(
            "SELECT week, day_index, done FROM workout_checks WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()

    def week_of(day_str: str) -> int:
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            return 0
        return max(1, (day - start0).days // 7 + 1)

    done_by_week: dict = {}
    for row in workouts:
        done_by_week.setdefault(week_of(row["done_on"]), set()).add(int(row["day_index"]))

    partial: dict = {}
    skipped: dict = {}
    for row in checks:
        week = int(row["week"])
        value = int(row["done"])
        if value == 2:
            partial.setdefault(week, set()).add(int(row["day_index"]))
        elif value == 0:
            skipped.setdefault(week, set()).add(int(row["day_index"]))

    weeks = []
    for week in range(current, max(0, current - limit), -1):
        done = len(done_by_week.get(week, set()))
        weeks.append({
            "week": week,
            "done": done,
            "partial": len(partial.get(week, set())),
            "skipped": len(skipped.get(week, set())),
            "deload": week % 4 == 0,
        })

    streak = 0
    for week in range(current, 0, -1):
        if done_by_week.get(week):
            streak += 1
        elif week == current:
            continue          # текущая неделя может быть ещё не начата
        else:
            break

    return {
        "weeks": weeks,
        "experience": current,
        "total": len(workouts),
        "streak": streak,
    }


def _day_details_sync(user_id: int, limit: int, offset: int = 0) -> list:
    import exercises as lib

    anchor = _anchor_sync(user_id)
    since = anchor.strftime("%Y-%m-%d") if anchor else "0000-01-01"

    conn = _open()
    try:
        workouts = conn.execute(
            "SELECT done_on, day_index FROM workouts WHERE user_id = ? AND done_on >= ?",
            (user_id, since),
        ).fetchall()
        checks = conn.execute(
            "SELECT week, day_index, done, created_at FROM workout_checks WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        failures = conn.execute(
            "SELECT week, day_index, slug, reason, created_at FROM failures WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    days: dict = {}

    def bucket(date: str, day_index: int) -> dict:
        key = (date, day_index)
        if key not in days:
            days[key] = {"date": date, "day_index": day_index, "status": "empty", "reasons": {}}
        return days[key]

    for row in workouts:
        if row["done_on"] >= since:
            bucket(row["done_on"], int(row["day_index"]))["status"] = "done"

    for row in checks:
        date = (row["created_at"] or "")[:10]
        if date < since:
            continue
        value = int(row["done"])
        item = bucket(date, int(row["day_index"]))
        if value == 0 and item["status"] != "done":
            item["status"] = "skip"
        elif value == 2 and item["status"] != "done":
            item["status"] = "partial"

    for row in failures:
        date = (row["created_at"] or "")[:10]
        if date < since:
            continue
        ex = lib.by_slug(row["slug"])
        group = ex.group if ex else "Другое"
        item = bucket(date, int(row["day_index"]))
        reasons = item["reasons"].setdefault(group, {})
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1

    ordered = sorted(days.values(), key=lambda item: (item["date"], item["day_index"]), reverse=True)
    return ordered[offset:offset + limit]


async def day_details(user_id: int, limit: int = 10, offset: int = 0) -> list:
    return await asyncio.to_thread(_day_details_sync, user_id, limit, offset)


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


async def history_weeks(user_id: int, limit: int = 8) -> dict:
    return await asyncio.to_thread(_history_weeks_sync, user_id, limit)


async def week_index(user_id: int) -> int:
    return await asyncio.to_thread(_week_index_sync, user_id)


async def week_bounds(user_id: int):
    return await asyncio.to_thread(_week_bounds_sync, user_id)


async def checks(user_id: int, week: int) -> dict:
    return await asyncio.to_thread(_checks_sync, user_id, week)


async def add_failures(user_id: int, week: int, day_index: int, items) -> None:
    await asyncio.to_thread(_add_failures_sync, user_id, week, day_index, items)


async def failures(user_id: int, days: int = 28):
    return await asyncio.to_thread(_failures_sync, user_id, days)


def _breakdown_sync(user_id: int, days: int) -> dict:
    import exercises as lib

    result: dict = {}
    conn = _open()
    try:
        since = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT slug, reason FROM failures WHERE user_id = ? AND created_at >= ?",
            (user_id, since),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        ex = lib.by_slug(row["slug"])
        group = ex.group if ex else "Другое"
        bucket = result.setdefault(group, {})
        bucket[row["reason"]] = bucket.get(row["reason"], 0) + 1
    return result


async def breakdown(user_id: int, days: int = 28) -> dict:
    return await asyncio.to_thread(_breakdown_sync, user_id, days)


async def weak_groups(user_id: int, days: int = 28) -> list:
    """Группы мышц, где чаще всего не получалось выполнить упражнение."""
    import exercises as lib

    rows = await failures(user_id, days)
    counts: dict = {}
    for row in rows:
        ex = lib.by_slug(row["slug"])
        group = ex.group if ex else "Другое"
        counts[group] = counts.get(group, 0) + 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)


async def week_done_days(user_id: int, start) -> set:
    return await asyncio.to_thread(_week_done_days_sync, user_id, start)


async def set_check(user_id: int, week: int, day_index: int, done) -> None:
    await asyncio.to_thread(_set_check_sync, user_id, week, day_index, done)


async def series(user_id: int, slug: str, days: int = 56):
    return await asyncio.to_thread(_series_sync, user_id, slug, days)


def _deload_reasons(week: int, checks: dict, done_days: set, planned: int) -> list:
    reasons = []
    if week % 4 == 0 and week > 0:
        reasons.append("плановая разгрузка")
    # Пропуск — только день, отмеченный «не смог» (0) и отсутствующий в журнале.
    # Частичное выполнение (2) пропуском не считается.
    misses = [day for day, value in checks.items() if value == 0 and day not in done_days]
    if len(misses) >= 2:
        reasons.append("две пропущенные тренировки")
    return reasons


async def week_state(user_id: int, planned_days: int) -> dict:
    """Неделя: номер, отметки, пропуски и нужна ли разгрузка."""
    week, start = await week_bounds(user_id)
    marks = await checks(user_id, week)
    done_days = await week_done_days(user_id, start)
    reasons = _deload_reasons(week, marks, done_days, planned_days)
    partial = [day for day, value in marks.items() if value == 2]
    return {
        "week": week,
        "start": start,
        "checks": marks,
        "partial": partial,
        "done": len(done_days),
        "planned": planned_days,
        "deload": bool(reasons),
        # после разгрузочной недели вес не повышаем: сначала закрепляем
        "after_deload": week > 1 and (week - 1) % 4 == 0,
        "reasons": reasons,
    }


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


def next_plan(goal: str, equipment: str, sets: Sequence[sqlite3.Row],
              allow_increase: bool = True) -> dict:
    """Вес и повторения на следующую тренировку (для подстановки в план).

    allow_increase=False — не повышаем вес: так бывает после разгрузочной
    недели и после частично выполненной тренировки.
    """
    if not sets:
        return {"weight": 0.0, "reps": None, "bodyweight": False}

    top = _top_reps(goal)
    weight = float(sets[-1]["weight"])
    best = max(int(s["reps"]) for s in sets)
    bodyweight = weight <= 0 or "свой вес" in (equipment or "").lower()

    if bodyweight:
        if not allow_increase:
            return {"weight": 0.0, "reps": best, "bodyweight": True}
        return {"weight": 0.0, "reps": best + 1, "bodyweight": True}
    if best >= top and allow_increase:
        return {"weight": weight + _step_for(equipment), "reps": top, "bodyweight": False}
    if not allow_increase:
        return {"weight": weight, "reps": max(best, top - 2), "bodyweight": False}
    return {"weight": weight, "reps": best + 1, "bodyweight": False}


def next_advice(goal: str, equipment: str, sets: Sequence[sqlite3.Row],
                allow_increase: bool = True) -> str:
    """Что делать на следующей тренировке с этим упражнением."""
    if not sets:
        return "первый раз — подберите вес так, чтобы оставалось 1–2 повторения в запасе"

    top = _top_reps(goal)
    weight = float(sets[-1]["weight"])
    best = max(int(s["reps"]) for s in sets)
    bodyweight = weight <= 0 or "свой вес" in (equipment or "").lower()

    if not allow_increase:
        if bodyweight:
            return f"закрепите: {best} повторений (вес тела)"
        return f"{weight:g} кг, закрепите результат"

    if bodyweight:
        if best >= top:
            return f"{best + 1} повторений (вес тела)"
        return f"{best + 1} повторений, добавьте подход"

    if best >= top:
        step = _step_for(equipment)
        return f"{weight + step:g} кг ({weight:g} → {weight + step:g})"
    return f"{weight:g} кг, повторите {best + 1}+ повторений"
