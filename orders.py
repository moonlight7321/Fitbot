"""Заявки на оплату переводом: QR СБП или реквизиты карты.

Схема ручная: пользователь получает сумму с уникальными копейками,
переводит, нажимает «Я оплатил», владелец подтверждает — и только тогда
продлевается подписка.

Заявка живёт ORDER_TTL_MINUTES (по умолчанию 60 минут): после истечения её
нужно оформлять заново, сумма освобождается для следующего человека.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))
ORDER_TTL_MINUTES = int(os.getenv("ORDER_TTL_MINUTES", "60"))
PRICE_RUB = float(os.getenv("SUBSCRIPTION_PRICE_RUB", "349").replace(",", "."))

PAYMENT_MODE = (os.getenv("PAYMENT_MODE", "qr") or "qr").strip().lower()
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "").strip()
PAYMENT_QR_PATH = Path(os.getenv("PAYMENT_QR_PATH", "payment_qr.png"))
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "").strip()

# Если путь не задан или файла нет — пробуем типовые имена рядом с ботом
_QR_FALLBACKS = ("payment_qr.png", "payment_qr.jpg", "payment_qr.jpeg")


def qr_file() -> Optional[Path]:
    """Файл QR, который реально существует, или None."""
    if PAYMENT_QR_PATH.exists():
        return PAYMENT_QR_PATH
    for name in _QR_FALLBACKS:
        candidate = Path(name)
        if candidate.exists():
            return candidate
    return None

STATUS_NEW = "new"
STATUS_WAITING = "waiting_review"
STATUS_PAID = "paid"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

ACTIVE_STATUSES = (STATUS_NEW, STATUS_WAITING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      REAL NOT NULL,
    days        INTEGER NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    paid_at     TEXT,
    reviewed_at TEXT,
    reviewed_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_id, status);
"""


class OrderExists(RuntimeError):
    """У пользователя уже есть активная заявка."""

    def __init__(self, order: "Order") -> None:
        super().__init__("у пользователя уже есть активная заявка")
        self.order = order


class OrderExpired(RuntimeError):
    """Заявка истекла — нужна новая."""


class OrderNotFound(RuntimeError):
    """Заявки нет."""


@dataclass
class Order:
    id: int
    user_id: int
    amount: float
    days: int
    status: str
    created_at: datetime
    expires_at: datetime
    paid_at: Optional[datetime] = None

    @property
    def short(self) -> str:
        return f"{self.id:04X}"

    @property
    def amount_str(self) -> str:
        return f"{self.amount:.2f}"

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or _now()) >= self.expires_at


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
        return None


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_order(row) -> Order:
    return Order(
        id=row["id"],
        user_id=row["user_id"],
        amount=row["amount"],
        days=row["days"],
        status=row["status"],
        created_at=_parse(row["created_at"]),
        expires_at=_parse(row["expires_at"]),
        paid_at=_parse(row["paid_at"]),
    )


def _expire_stale(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "UPDATE orders SET status = ? WHERE status IN (?, ?) AND expires_at <= ?",
        (STATUS_EXPIRED, STATUS_NEW, STATUS_WAITING, _fmt(now)),
    )


def _active_amounts(conn: sqlite3.Connection, now: datetime) -> set:
    rows = conn.execute(
        "SELECT amount FROM orders WHERE status IN (?, ?) AND expires_at > ?",
        (STATUS_NEW, STATUS_WAITING, _fmt(now)),
    ).fetchall()
    return {round(r["amount"], 2) for r in rows}


def _pick_amount(base: float, taken: set) -> float:
    """349.00 -> 349.07, 349.14 ... Уникальные копейки, чтобы найти перевод."""
    start = int(round(base * 100))
    for step in range(1, 9901):       # 99 копеек x 100 рублей
        candidate = (start + step) / 100
        if round(candidate, 2) not in taken:
            return round(candidate, 2)
    raise RuntimeError("не осталось свободных сумм")


# --- синхронные операции ----------------------------------------------------

def _create_order_sync(user_id: int, days: int) -> Order:
    now = _now()
    conn = _open()
    try:
        _expire_stale(conn, now)

        row = conn.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
            (user_id, STATUS_NEW, STATUS_WAITING),
        ).fetchone()
        if row is not None:
            raise OrderExists(_row_to_order(row))

        amount = _pick_amount(PRICE_RUB, _active_amounts(conn, now))
        expires_at = now + timedelta(minutes=ORDER_TTL_MINUTES)
        cur = conn.execute(
            """INSERT INTO orders (user_id, amount, days, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, amount, days, STATUS_NEW, _fmt(now), _fmt(expires_at)),
        )
        conn.commit()
        order = _row_to_order(
            conn.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone()
        )
        logger.info("Заявка #%s: user=%s, %s руб, до %s", order.short, user_id, order.amount_str, _fmt(expires_at))
        return order
    finally:
        conn.close()


def _get_sync(order_id: int) -> Optional[Order]:
    conn = _open()
    try:
        _expire_stale(conn, _now())
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return _row_to_order(row) if row else None
    finally:
        conn.close()


def _mark_paid_sync(order_id: int, user_id: int) -> Order:
    now = _now()
    conn = _open()
    try:
        _expire_stale(conn, now)
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None or row["user_id"] != user_id:
            raise OrderNotFound("заявка не найдена")
        order = _row_to_order(row)
        if order.status == STATUS_EXPIRED or order.is_expired(now):
            conn.commit()
            raise OrderExpired("заявка истекла")
        if order.status == STATUS_PAID:
            return order
        if order.status == STATUS_REJECTED:
            raise OrderExpired("заявка отклонена")
        conn.execute(
            "UPDATE orders SET status = ?, paid_at = ? WHERE id = ?",
            (STATUS_WAITING, _fmt(now), order_id),
        )
        conn.commit()
        logger.info("Заявка #%s: пользователь сообщил об оплате", order.short)
        return _row_to_order(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
    finally:
        conn.close()


def _review_sync(order_id: int, reviewer: int, approve: bool) -> Order:
    now = _now()
    conn = _open()
    try:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise OrderNotFound("заявка не найдена")
        order = _row_to_order(row)
        if order.status == STATUS_PAID:
            return order                      # идемпотентно
        if order.status == STATUS_REJECTED:
            raise OrderExpired("заявка уже отклонена")
        # Подтвердить можно и просроченную: перевод мог уйти за минуту до
        # дедлайна, а кнопку человек нажал позже. Отклонить просроченную — нет.
        if not approve and order.status == STATUS_EXPIRED:
            raise OrderExpired("заявка истекла")

        conn.execute(
            "UPDATE orders SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
            (STATUS_PAID if approve else STATUS_REJECTED, _fmt(now), reviewer, order_id),
        )
        conn.commit()
        logger.info("Заявка #%s: %s, reviewer=%s", order.short, "подтверждена" if approve else "отклонена", reviewer)
        return _row_to_order(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
    finally:
        conn.close()


def _expire_all_sync() -> int:
    now = _now()
    conn = _open()
    try:
        _expire_stale(conn, now)
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


# --- асинхронная обёртка ----------------------------------------------------

async def create_order(user_id: int, days: int) -> Order:
    return await asyncio.to_thread(_create_order_sync, user_id, days)


async def get(order_id: int) -> Optional[Order]:
    return await asyncio.to_thread(_get_sync, order_id)


async def mark_paid(order_id: int, user_id: int) -> Order:
    return await asyncio.to_thread(_mark_paid_sync, order_id, user_id)


async def review(order_id: int, reviewer: int, approve: bool) -> Order:
    return await asyncio.to_thread(_review_sync, order_id, reviewer, approve)


async def expire_all() -> int:
    return await asyncio.to_thread(_expire_all_sync)
