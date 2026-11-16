"""Резервные копии SQLite-базы и восстановление.

На бесплатном тарифе Bothost база живёт внутри контейнера и стирается при
каждом Redeploy. Копия отправляется владельцу в Telegram (файл .db), а
восстанавливается пересылкой этого файла боту.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "fitbot.db"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))
SQLITE_HEADER = b"SQLite format 3\x00"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _rotate(prefix: str) -> None:
    # Сортируем по времени, а не по имени: суффикс -2 меняет лексикографический
    # порядок и мог удалить только что созданную копию
    files = sorted(
        BACKUP_DIR.glob(f"{prefix}-*.db"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    for old in files[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            logger.debug("Не удалось удалить старую копию %s", old)


def make_backup_sync() -> Optional[Path]:
    """Согласованная копия через sqlite backup API (работает и в режиме WAL)."""
    if not DB_PATH.exists():
        logger.warning("Бэкап не сделан: %s не существует", DB_PATH)
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"{DB_PATH.stem}-{_stamp()}.db"
    # Две копии в одну секунду (например, два платежа подряд) не должны
    # затирать друг друга
    counter = 1
    while target.exists():
        counter += 1
        target = BACKUP_DIR / f"{DB_PATH.stem}-{_stamp()}-{counter}.db"

    source = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(target)
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    size_kb = target.stat().st_size // 1024
    _rotate(DB_PATH.stem)
    logger.info("Бэкап базы: %s (%s КБ)", target, size_kb)
    return target


def restore_sync(data: bytes) -> Path:
    """Проверяет и подменяет базу. Текущая сохраняется как *.before-restore.db."""
    if not data or not data.startswith(SQLITE_HEADER):
        raise ValueError("файл не похож на базу SQLite")

    tmp = DB_PATH.with_name(DB_PATH.name + ".restore.tmp")
    tmp.write_bytes(data)

    try:
        conn = sqlite3.connect(tmp)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"база повреждена: {exc}") from exc

    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise ValueError(f"проверка целостности не прошла: {check}")

    if not {"subscriptions", "orders"} & tables:
        tmp.unlink(missing_ok=True)
        raise ValueError("в файле нет таблиц подписок и заявок")

    previous = DB_PATH.with_name(DB_PATH.name + ".before-restore.db")
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, previous)

    tmp.replace(DB_PATH)
    logger.info("База восстановлена из файла (%s КБ), таблиц: %s", len(data) // 1024, len(tables))
    return previous


async def make_backup() -> Optional[Path]:
    return await asyncio.to_thread(make_backup_sync)


async def restore(data: bytes) -> Path:
    return await asyncio.to_thread(restore_sync, data)
