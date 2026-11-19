"""Анкета пользователя: параметры для построения плана."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id    INTEGER PRIMARY KEY,
    height     INTEGER NOT NULL,
    weight     INTEGER NOT NULL,
    experience TEXT NOT NULL,
    days       INTEGER NOT NULL,
    minutes    INTEGER NOT NULL,
    goal       TEXT NOT NULL,
    equipment  TEXT NOT NULL,
    limitations TEXT DEFAULT '',
    sex        TEXT DEFAULT '',
    age        INTEGER DEFAULT 0,
    activity   TEXT DEFAULT '',
    workout_days TEXT DEFAULT '',
    workout_time TEXT DEFAULT '',
    last_reminder TEXT DEFAULT '',
    day_cursor INTEGER DEFAULT -1,
    weight_reminder INTEGER DEFAULT 0,
    last_weight_reminder TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

ACTIVITY = {
    "sedentary": "Сидячая работа, мало движения",
    "light": "Иногда на ногах",
    "active": "Весь день на ногах",
    "heavy": "Тяжёлая физическая работа",
}
LIMITATIONS = {
    "knees": "Колени",
    "back": "Спина, поясница",
    "shoulders": "Плечи",
    "wrists": "Запястья",
    "heart": "Сердце, давление",
}
GOALS = {
    "mass": "Набрать массу",
    "relief": "Рельеф",
    "tone": "Тонус и здоровье",
}
EXPERIENCE = {
    "novice": "Новичок (до года)",
    "middle": "Средний (1–3 года)",
    "advanced": "Продвинутый (3+ года)",
}
EQUIPMENT = {
    "gym": "Зал",
    "home": "Дом с гантелями",
    "body": "Только свой вес",
}
DAYS_OPTIONS = (2, 3, 4, 5)
MINUTES_OPTIONS = (20, 30, 45, 60)


@dataclass
class Profile:
    user_id: int
    height: int
    weight: int
    experience: str
    days: int
    minutes: int
    goal: str
    equipment: str
    limitations: str = ""
    sex: str = ""
    age: int = 0
    activity: str = ""
    workout_days: str = ""
    workout_time: str = ""
    last_reminder: str = ""
    day_cursor: int = -1
    created_at: str = ""
    weight_reminder: bool = False
    last_weight_reminder: str = ""
    replacements: dict = dataclass_field(default_factory=dict)

    @property
    def weekdays(self) -> list:
        return sorted({int(x) for x in (self.workout_days or "").split(",") if x.strip().isdigit()})

    @property
    def has_reminders(self) -> bool:
        return bool(self.weekdays) and bool(self.workout_time)

    @property
    def activity_title(self) -> str:
        return ACTIVITY.get(self.activity, "не указана")

    @property
    def has_body_data(self) -> bool:
        return self.sex in ("m", "f") and self.age > 0

    @property
    def limits(self) -> set:
        return {x.strip() for x in (self.limitations or "").split(",") if x.strip()}

    @property
    def limitations_title(self) -> str:
        names = [LIMITATIONS[c] for c in self.limits if c in LIMITATIONS]
        return ", ".join(names) if names else "нет"

    @property
    def goal_title(self) -> str:
        return GOALS.get(self.goal, self.goal)

    @property
    def experience_title(self) -> str:
        return EXPERIENCE.get(self.experience, self.experience)

    @property
    def equipment_title(self) -> str:
        return EQUIPMENT.get(self.equipment, self.equipment)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    # колонка появилась позже: старые базы её не содержат
    columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
    migrations = {
        "limitations": "ALTER TABLE profiles ADD COLUMN limitations TEXT DEFAULT ''",
        "sex": "ALTER TABLE profiles ADD COLUMN sex TEXT DEFAULT ''",
        "age": "ALTER TABLE profiles ADD COLUMN age INTEGER DEFAULT 0",
        "activity": "ALTER TABLE profiles ADD COLUMN activity TEXT DEFAULT ''",
        "workout_days": "ALTER TABLE profiles ADD COLUMN workout_days TEXT DEFAULT ''",
        "workout_time": "ALTER TABLE profiles ADD COLUMN workout_time TEXT DEFAULT ''",
        "last_reminder": "ALTER TABLE profiles ADD COLUMN last_reminder TEXT DEFAULT ''",
        "day_cursor": "ALTER TABLE profiles ADD COLUMN day_cursor INTEGER DEFAULT -1",
        "weight_reminder": "ALTER TABLE profiles ADD COLUMN weight_reminder INTEGER DEFAULT 0",
        "last_weight_reminder": "ALTER TABLE profiles ADD COLUMN last_weight_reminder TEXT DEFAULT ''",
        "created_at": "ALTER TABLE profiles ADD COLUMN created_at TEXT DEFAULT ''",
    }
    added = False
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
            added = True
    if added:
        conn.commit()

    # Старые анкеты: датой создания считаем последнее обновление
    conn.execute(
        "UPDATE profiles SET created_at = updated_at WHERE created_at IS NULL OR created_at = ''"
    )
    conn.commit()
    return conn


def _get_sync(user_id: int) -> Optional[Profile]:
    conn = _open()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    # Читаем по именам: в старой базе колонка limitations добавлена в конец,
    # и позиция row[8] там — это updated_at, а не ограничения.
    keys = set(row.keys())
    replacements: dict = {}
    try:
        repl_conn = _open()
        try:
            for item in repl_conn.execute(
                "SELECT day_index, original, replacement FROM day_replacements WHERE user_id = ?",
                (user_id,),
            ).fetchall():
                replacements.setdefault(int(item["day_index"]), {})[item["original"]] = item["replacement"]
        finally:
            repl_conn.close()
    except sqlite3.Error:
        replacements = {}

    return Profile(
        user_id=row["user_id"], height=row["height"], weight=row["weight"],
        experience=row["experience"], days=row["days"], minutes=row["minutes"],
        goal=row["goal"], equipment=row["equipment"],
        limitations=((row["limitations"] if "limitations" in keys else "") or ""),
        sex=((row["sex"] if "sex" in keys else "") or ""),
        age=int((row["age"] if "age" in keys else 0) or 0),
        activity=((row["activity"] if "activity" in keys else "") or ""),
        workout_days=((row["workout_days"] if "workout_days" in keys else "") or ""),
        workout_time=((row["workout_time"] if "workout_time" in keys else "") or ""),
        last_reminder=((row["last_reminder"] if "last_reminder" in keys else "") or ""),
        day_cursor=(
            int(row["day_cursor"])
            if "day_cursor" in keys and row["day_cursor"] is not None
            else -1
        ),
        weight_reminder=bool(int(row["weight_reminder"] or 0)) if "weight_reminder" in keys else False,
        last_weight_reminder=((row["last_weight_reminder"] if "last_weight_reminder" in keys else "") or ""),
        created_at=((row["created_at"] if "created_at" in keys else "") or ""),
        replacements=replacements,
    )


def _save_sync(profile: Profile) -> None:
    conn = _open()
    try:
        conn.execute(
            """
            INSERT INTO profiles (user_id, height, weight, experience, days, minutes,
                                  goal, equipment, limitations, sex, age, activity,
                                  workout_days, workout_time, last_reminder, day_cursor,
                                  weight_reminder, last_weight_reminder, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                height = excluded.height, weight = excluded.weight,
                experience = excluded.experience, days = excluded.days,
                minutes = excluded.minutes, goal = excluded.goal,
                equipment = excluded.equipment, limitations = excluded.limitations,
                sex = excluded.sex, age = excluded.age, activity = excluded.activity,
                workout_days = excluded.workout_days, workout_time = excluded.workout_time,
                last_reminder = excluded.last_reminder, day_cursor = excluded.day_cursor,
                weight_reminder = excluded.weight_reminder,
                last_weight_reminder = excluded.last_weight_reminder,
                updated_at = excluded.updated_at
            """,
            (profile.user_id, profile.height, profile.weight, profile.experience,
             profile.days, profile.minutes, profile.goal, profile.equipment,
             profile.limitations, profile.sex, profile.age, profile.activity,
             profile.workout_days, profile.workout_time, profile.last_reminder,
             profile.day_cursor, 1 if profile.weight_reminder else 0,
             profile.last_weight_reminder, profile.created_at or _now(), _now()),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Анкета сохранена: user=%s, %s см, %s кг, %s", profile.user_id, profile.height, profile.weight, profile.goal)


async def get(user_id: int) -> Optional[Profile]:
    return await asyncio.to_thread(_get_sync, user_id)


async def save(profile: Profile) -> None:
    await asyncio.to_thread(_save_sync, profile)


def _with_reminders_sync() -> list:
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT * FROM profiles WHERE workout_days != '' AND workout_time != ''"
        ).fetchall()
        return [
            Profile(
                user_id=r["user_id"], height=r["height"], weight=r["weight"],
                experience=r["experience"], days=r["days"], minutes=r["minutes"],
                goal=r["goal"], equipment=r["equipment"],
                limitations=r["limitations"] or "", sex=r["sex"] or "",
                age=int(r["age"] or 0), activity=r["activity"] or "",
                workout_days=r["workout_days"] or "", workout_time=r["workout_time"] or "",
                last_reminder=r["last_reminder"] or "",
            )
            for r in rows
        ]
    finally:
        conn.close()


def _mark_reminder_sync(user_id: int, day: str) -> None:
    conn = _open()
    try:
        conn.execute("UPDATE profiles SET last_reminder = ? WHERE user_id = ?", (day, user_id))
        conn.commit()
    finally:
        conn.close()


def _with_weight_reminders_sync() -> list:
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT * FROM profiles WHERE weight_reminder = 1"
        ).fetchall()
        result = []
        for row in rows:
            result.append(Profile(
                user_id=row["user_id"], height=row["height"], weight=row["weight"],
                experience=row["experience"], days=row["days"], minutes=row["minutes"],
                goal=row["goal"], equipment=row["equipment"],
                limitations=row["limitations"] or "", sex=row["sex"] or "",
                age=int(row["age"] or 0), activity=row["activity"] or "",
                workout_days=row["workout_days"] or "", workout_time=row["workout_time"] or "",
                last_reminder=row["last_reminder"] or "",
                day_cursor=int(row["day_cursor"] or -1) if row["day_cursor"] is not None else -1,
                weight_reminder=True,
                last_weight_reminder=row["last_weight_reminder"] or "",
            ))
        return result
    finally:
        conn.close()


def _mark_weight_reminder_sync(user_id: int, day: str) -> None:
    conn = _open()
    try:
        conn.execute("UPDATE profiles SET last_weight_reminder = ? WHERE user_id = ?", (day, user_id))
        conn.commit()
    finally:
        conn.close()


async def with_weight_reminders() -> list:
    return await asyncio.to_thread(_with_weight_reminders_sync)


async def mark_weight_reminder(user_id: int, day: str) -> None:
    await asyncio.to_thread(_mark_weight_reminder_sync, user_id, day)


async def with_reminders() -> list:
    return await asyncio.to_thread(_with_reminders_sync)


async def mark_reminder(user_id: int, day: str) -> None:
    await asyncio.to_thread(_mark_reminder_sync, user_id, day)
