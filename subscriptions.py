"""Подписка: пробный период и оплата Telegram Stars (XTR).

Оплата цифровых товаров в Telegram разрешена только звёздами: currency="XTR",
provider_token пустой. Пользователь при этом платит обычной картой, просто
пополняя Stars внутри Telegram.

Состояние хранится в SQLite (DB_PATH, по умолчанию fitbot.db рядом с ботом).
Доступ = paid_until или trial_ends_at в будущем.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))
PRICE_STARS = int(os.getenv("SUBSCRIPTION_PRICE_STARS", "250"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))

# Продажи включаются отдельным флагом: пока платных функций нет,
# случайный человек не должен купить доступ в пустоту.
SALES_ENABLED = (os.getenv("SALES_ENABLED", "false") or "").strip().lower() in {
    "1", "true", "yes", "y", "on", "да",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id          INTEGER PRIMARY KEY,
    trial_started_at TEXT,
    trial_ends_at    TEXT,
    paid_until       TEXT,
    plan             TEXT,
    charge_id        TEXT,
    reminded_paid_for  TEXT DEFAULT '',
    reminded_trial_for TEXT DEFAULT '',
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id  TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    days       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


class TrialAlreadyUsed(RuntimeError):
    """Пробный период уже был — повторно не выдаём."""


@dataclass
class Status:
    user_id: int
    trial_used: bool
    trial_ends_at: Optional[datetime]
    paid_until: Optional[datetime]
    active: bool
    days_left: int
    kind: str  # paid | trial | expired | none


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        logger.warning("Не разобрал дату подписки: %r", value)
        return None


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # колонки появились позже: старые базы их не содержат
    columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
    added = False
    for column in ("reminded_paid_for", "reminded_trial_for"):
        if column not in columns:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} TEXT DEFAULT ''")
            added = True
    if added:
        conn.commit()
    return conn


def _status_from_row(user_id: int, row) -> Status:
    now = _now()
    trial_end = _parse(row["trial_ends_at"]) if row else None
    paid_until = _parse(row["paid_until"]) if row else None

    dates = [d for d in (trial_end, paid_until) if d is not None]
    active_until = max(dates) if dates else None

    if active_until and active_until > now:
        kind = "paid" if (paid_until and paid_until > now) else "trial"
    elif active_until:
        kind = "expired"
    else:
        kind = "none"

    days_left = 0
    if active_until and active_until > now:
        days_left = ceil((active_until - now).total_seconds() / 86400)

    return Status(
        user_id=user_id,
        trial_used=trial_end is not None,
        trial_ends_at=trial_end,
        paid_until=paid_until,
        active=kind in ("paid", "trial"),
        days_left=days_left,
        kind=kind,
    )


def _read(conn: sqlite3.Connection, user_id: int):
    return conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()


# --- синхронная реализация (вызывается из потоков) --------------------------

def _get_status_sync(user_id: int) -> Status:
    conn = _open()
    try:
        return _status_from_row(user_id, _read(conn, user_id))
    finally:
        conn.close()


def _start_trial_sync(user_id: int) -> Status:
    now = _now()
    trial_end = now + timedelta(days=TRIAL_DAYS)
    conn = _open()
    try:
        row = _read(conn, user_id)
        if row is not None and row["trial_started_at"]:
            raise TrialAlreadyUsed("пробный период уже использован")
        conn.execute(
            """
            INSERT INTO subscriptions (user_id, trial_started_at, trial_ends_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                trial_started_at = excluded.trial_started_at,
                trial_ends_at    = excluded.trial_ends_at,
                updated_at       = excluded.updated_at
            """,
            (user_id, _fmt(now), _fmt(trial_end), _fmt(now)),
        )
        conn.commit()
        logger.info("Пробный период выдан: user=%s до %s", user_id, _fmt(trial_end))
        return _status_from_row(user_id, _read(conn, user_id))
    finally:
        conn.close()


def _add_paid_days_sync(user_id: int, days: int, charge_id: str, amount: int) -> Status:
    now = _now()
    conn = _open()
    try:
        # Идемпотентность: одно и то же successful_payment может прийти дважды
        if conn.execute("SELECT 1 FROM payments WHERE charge_id = ?", (charge_id,)).fetchone():
            logger.info("Платёж %s уже учтён, продление пропущено", charge_id)
            return _status_from_row(user_id, _read(conn, user_id))

        row = _read(conn, user_id)
        current = _parse(row["paid_until"]) if row else None
        trial_end = _parse(row["trial_ends_at"]) if row else None

        # Оплата во время триала не должна «съедать» оставшиеся дни:
        # считаем от самой поздней из дат (оплата, триал, сегодня).
        candidates = [now]
        if current and current > now:
            candidates.append(current)
        if trial_end and trial_end > now:
            candidates.append(trial_end)
        base = max(candidates)
        new_until = base + timedelta(days=days)

        conn.execute(
            """
            INSERT INTO subscriptions (user_id, paid_until, plan, charge_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                paid_until = excluded.paid_until,
                plan       = excluded.plan,
                charge_id  = excluded.charge_id,
                updated_at = excluded.updated_at
            """,
            (user_id, _fmt(new_until), f"stars:{days}d", charge_id, _fmt(now)),
        )
        conn.execute(
            "INSERT INTO payments (charge_id, user_id, amount, days, created_at) VALUES (?, ?, ?, ?, ?)",
            (charge_id, user_id, amount, days, _fmt(now)),
        )
        conn.commit()
        logger.info("Оплата учтена: user=%s charge=%s +%s дн. до %s", user_id, charge_id, days, _fmt(new_until))
        return _status_from_row(user_id, _read(conn, user_id))
    finally:
        conn.close()


def _revoke_sync(user_id: int) -> Status:
    now = _now()
    conn = _open()
    try:
        conn.execute(
            "UPDATE subscriptions SET trial_ends_at = NULL, paid_until = NULL, updated_at = ? WHERE user_id = ?",
            (_fmt(now), user_id),
        )
        conn.commit()
        logger.info("Доступ отозван: user=%s", user_id)
        return _status_from_row(user_id, _read(conn, user_id))
    finally:
        conn.close()


# --- асинхронное API (не блокируем event loop sqlite-запросами) --------------

async def get_status(user_id: int) -> Status:
    return await asyncio.to_thread(_get_status_sync, user_id)


async def start_trial(user_id: int) -> Status:
    return await asyncio.to_thread(_start_trial_sync, user_id)


async def add_paid_days(user_id: int, days: int, charge_id: str, amount: int) -> Status:
    return await asyncio.to_thread(_add_paid_days_sync, user_id, days, charge_id, amount)


async def revoke(user_id: int) -> Status:
    return await asyncio.to_thread(_revoke_sync, user_id)


def _due_sync(within_days: int) -> list:
    """Кому пора напомнить о скором окончании доступа."""
    now = _now()
    limit = now + timedelta(days=within_days)
    conn = _open()
    try:
        rows = conn.execute("SELECT * FROM subscriptions").fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        keys = set(row.keys())
        paid = _parse(row["paid_until"])
        trial = _parse(row["trial_ends_at"])
        reminded_paid = (row["reminded_paid_for"] if "reminded_paid_for" in keys else "") or ""
        reminded_trial = (row["reminded_trial_for"] if "reminded_trial_for" in keys else "") or ""

        if paid and now < paid <= limit and _fmt(paid) != reminded_paid:
            result.append({
                "user_id": row["user_id"], "kind": "paid", "until": paid,
                "days_left": ceil((paid - now).total_seconds() / 86400),
                "until_iso": _fmt(paid),
            })
        if trial and now < trial <= limit and _fmt(trial) != reminded_trial:
            result.append({
                "user_id": row["user_id"], "kind": "trial", "until": trial,
                "days_left": ceil((trial - now).total_seconds() / 86400),
                "until_iso": _fmt(trial),
            })
    return result


def _mark_reminder_sync(user_id: int, kind: str, until_iso: str) -> None:
    column = "reminded_paid_for" if kind == "paid" else "reminded_trial_for"
    conn = _open()
    try:
        conn.execute(
            f"UPDATE subscriptions SET {column} = ?, updated_at = ? WHERE user_id = ?",
            (until_iso, _now().strftime("%Y-%m-%dT%H:%M:%S"), user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def due_reminders(within_days: int) -> list:
    return await asyncio.to_thread(_due_sync, within_days)


async def mark_reminder(user_id: int, kind: str, until_iso: str) -> None:
    await asyncio.to_thread(_mark_reminder_sync, user_id, kind, until_iso)


async def has_access(user_id: int) -> bool:
    """Единая точка проверки для бота и API мини-аппа."""
    return (await get_status(user_id)).active
