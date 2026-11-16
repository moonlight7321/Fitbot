"""Анкета пользователя: параметры для построения плана."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
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
    updated_at TEXT NOT NULL
);
"""

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
    if "limitations" not in columns:
        conn.execute("ALTER TABLE profiles ADD COLUMN limitations TEXT DEFAULT ''")
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
    return Profile(
        user_id=row["user_id"], height=row["height"], weight=row["weight"],
        experience=row["experience"], days=row["days"], minutes=row["minutes"],
        goal=row["goal"], equipment=row["equipment"],
        limitations=((row["limitations"] if "limitations" in keys else "") or ""),
    )


def _save_sync(profile: Profile) -> None:
    conn = _open()
    try:
        conn.execute(
            """
            INSERT INTO profiles (user_id, height, weight, experience, days, minutes, goal, equipment, limitations, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                height = excluded.height, weight = excluded.weight,
                experience = excluded.experience, days = excluded.days,
                minutes = excluded.minutes, goal = excluded.goal,
                equipment = excluded.equipment, limitations = excluded.limitations,
                updated_at = excluded.updated_at
            """,
            (profile.user_id, profile.height, profile.weight, profile.experience,
             profile.days, profile.minutes, profile.goal, profile.equipment,
             profile.limitations, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Анкета сохранена: user=%s, %s см, %s кг, %s", profile.user_id, profile.height, profile.weight, profile.goal)


async def get(user_id: int) -> Optional[Profile]:
    return await asyncio.to_thread(_get_sync, user_id)


async def save(profile: Profile) -> None:
    await asyncio.to_thread(_save_sync, profile)
