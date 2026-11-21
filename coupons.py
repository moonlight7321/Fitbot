"""Купоны на первую оплату.

Каждому пользователю при первом /start выдаётся одноразовый купон
на COUPON_PERCENT % (по умолчанию 50) сроком COUPON_DAYS дней.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

import dates

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))
COUPON_PERCENT = int(os.getenv("COUPON_PERCENT", "50"))
COUPON_DAYS = int(os.getenv("COUPON_DAYS", "7"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coupons (
    user_id    INTEGER PRIMARY KEY,
    code       TEXT NOT NULL,
    percent    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
"""


@dataclass
class Coupon:
    user_id: int
    code: str
    percent: int
    expires_at: datetime
    used_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.now(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_coupon(row) -> Optional[Coupon]:
    if row is None:
        return None
    expires = dates.parse_dt(row["expires_at"])
    if expires is None:
        return None
    return Coupon(
        user_id=row["user_id"],
        code=row["code"],
        percent=int(row["percent"]),
        expires_at=expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires,
        used_at=dates.parse_dt(row["used_at"]),
    )


def _ensure_sync(user_id: int) -> Tuple[Coupon, bool]:
    now = _now()
    conn = _open()
    try:
        row = conn.execute("SELECT * FROM coupons WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None:
            coupon = _row_to_coupon(row)
            if coupon is not None:
                return coupon, False

        code = "START" + secrets.token_hex(2).upper()
        expires = now + timedelta(days=COUPON_DAYS)
        conn.execute(
            """INSERT INTO coupons (user_id, code, percent, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, code, COUPON_PERCENT, dates.format_dt(now), dates.format_dt(expires)),
        )
        conn.commit()
        logger.info("Купон выдан: user=%s, %s%%, до %s", user_id, COUPON_PERCENT, expires.date())
        return Coupon(user_id, code, COUPON_PERCENT, expires), True
    finally:
        conn.close()


def _get_sync(user_id: int) -> Optional[Coupon]:
    conn = _open()
    try:
        return _row_to_coupon(
            conn.execute("SELECT * FROM coupons WHERE user_id = ?", (user_id,)).fetchone()
        )
    finally:
        conn.close()


def _mark_used_sync(user_id: int) -> None:
    conn = _open()
    try:
        conn.execute(
            "UPDATE coupons SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (dates.format_dt(_now()), user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def ensure(user_id: int) -> Tuple[Coupon, bool]:
    return await asyncio.to_thread(_ensure_sync, user_id)


async def get(user_id: int) -> Optional[Coupon]:
    return await asyncio.to_thread(_get_sync, user_id)


async def active(user_id: int) -> Optional[Coupon]:
    coupon = await get(user_id)
    return coupon if coupon and coupon.active else None


async def mark_used(user_id: int) -> None:
    await asyncio.to_thread(_mark_used_sync, user_id)
