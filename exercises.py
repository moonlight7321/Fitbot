"""Библиотека упражнений: данные из exercises.json, картинки из media/.

Источник данных — free-exercise-db (Unlicense), но тексты, подсказки и ошибки
написаны заново по-русски, чтобы не тащить в канал чужой устаревший текст.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = Path(os.getenv("EXERCISES_PATH", BASE_DIR / "exercises.json"))
MEDIA_DIR = Path(os.getenv("EXERCISE_MEDIA_DIR", BASE_DIR / "media" / "exercises"))
DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_cache (
    slug    TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    PRIMARY KEY (slug, idx)
);
"""


@dataclass(frozen=True)
class Exercise:
    slug: str
    name_ru: str
    group: str
    level: str
    equipment: str
    muscles: List[str]
    cues: List[str]
    mistakes: List[str]
    safety: str
    free: bool

    def images(self) -> List[Path]:
        folder = MEDIA_DIR / self.slug
        found = sorted(folder.glob("*.jpg"))
        return found[:2]

    def card_text(self) -> str:
        cues = "\n".join(f"{i}. {c}" for i, c in enumerate(self.cues, 1))
        mistakes = "\n".join(f"✗ {m}" for m in self.mistakes)
        muscles = ", ".join(self.muscles)
        return (
            f"<b>{self.name_ru}</b>\n"
            f"{self.level} · {self.equipment}\n"
            f"Мышцы: {muscles}\n\n"
            f"<b>Как делать</b>\n{cues}\n\n"
            f"<b>Частые ошибки</b>\n{mistakes}\n\n"
            f"<i>{self.safety}</i>"
        )


def _load() -> List[Exercise]:
    if not DATA_PATH.exists():
        logger.warning("Нет файла упражнений: %s", DATA_PATH)
        return []
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    result = []
    for item in raw:
        try:
            result.append(
                Exercise(
                    slug=item["slug"],
                    name_ru=item["name_ru"],
                    group=item["group"],
                    level=item["level"],
                    equipment=item.get("equipment", ""),
                    muscles=list(item.get("muscles", [])),
                    cues=list(item.get("cues", [])),
                    mistakes=list(item.get("mistakes", [])),
                    safety=item.get("safety", ""),
                    free=bool(item.get("free", False)),
                )
            )
        except KeyError as exc:
            logger.error("Битое упражнение в %s: нет поля %s", DATA_PATH, exc)
    return result


EXERCISES: List[Exercise] = _load()
_BY_SLUG: Dict[str, Exercise] = {e.slug: e for e in EXERCISES}
GROUP_ORDER = ["Грудь", "Спина", "Ноги", "Плечи", "Руки", "Пресс"]


def all_exercises() -> List[Exercise]:
    return EXERCISES


def groups() -> List[str]:
    present = {e.group for e in EXERCISES}
    ordered = [g for g in GROUP_ORDER if g in present]
    return ordered + sorted(present - set(ordered))


def by_group(group: str) -> List[Exercise]:
    return [e for e in EXERCISES if e.group == group]


def _norm_name(value: str) -> str:
    return (value or "").strip().lower().replace("ё", "е")


def name_candidates(query: str) -> List[Exercise]:
    """Кандидаты по названию: точное, начало, подстрока, совпадение слов.

    Слова сравниваем по первым пяти буквам — «штангой» и «штанги» должны
    считаться одним словом, иначе «жим лёжа со штангой» не находится.
    """
    q = _norm_name(query)
    if not q:
        return []
    items = all_exercises()

    exact = [ex for ex in items if _norm_name(ex.name_ru) == q]
    if exact:
        return exact
    prefix = [ex for ex in items if _norm_name(ex.name_ru).startswith(q)]
    if prefix:
        return prefix
    contains = [ex for ex in items if q in _norm_name(ex.name_ru)]
    if contains:
        return contains

    tokens = [t for t in q.split() if len(t) > 2]
    if len(tokens) > 1:
        found = [
            ex for ex in items
            if all(t[:5] in _norm_name(ex.name_ru) for t in tokens)
        ]
        if found:
            return sorted(found, key=lambda ex: len(ex.name_ru))
    return []


def find_by_name(query: str) -> Optional[Exercise]:
    candidates = name_candidates(query)
    return candidates[0] if candidates else None


def find_by_name_among(query: str, slugs) -> Optional[Exercise]:
    """Тот же поиск, но сначала пробуем упражнения из текущего дня."""
    allowed = set(slugs)
    candidates = name_candidates(query)
    for ex in candidates:
        if ex.slug in allowed:
            return ex
    return candidates[0] if candidates else None


def by_slug(slug: str) -> Optional[Exercise]:
    return _BY_SLUG.get(slug)


# --- кэш file_id, чтобы не загружать картинки в Telegram повторно -------------

def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def _ids_sync(slug: str) -> Dict[int, str]:
    conn = _open()
    try:
        rows = conn.execute("SELECT idx, file_id FROM media_cache WHERE slug = ?", (slug,)).fetchall()
        return {int(idx): str(fid) for idx, fid in rows}
    finally:
        conn.close()


def _save_ids_sync(slug: str, ids: Dict[int, str]) -> None:
    if not ids:
        return
    conn = _open()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO media_cache (slug, idx, file_id) VALUES (?, ?, ?)",
            [(slug, idx, fid) for idx, fid in ids.items()],
        )
        conn.commit()
    finally:
        conn.close()


async def cached_file_ids(slug: str) -> Dict[int, str]:
    return await asyncio.to_thread(_ids_sync, slug)


def _forget_ids_sync(slug: str) -> None:
    conn = _open()
    try:
        conn.execute("DELETE FROM media_cache WHERE slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()


async def remember_file_ids(slug: str, ids: Dict[int, str]) -> None:
    await asyncio.to_thread(_save_ids_sync, slug, ids)


async def forget_file_ids(slug: str) -> None:
    """Сбрасывает кэш file_id: они могли устареть или быть от другого бота."""
    await asyncio.to_thread(_forget_ids_sync, slug)
