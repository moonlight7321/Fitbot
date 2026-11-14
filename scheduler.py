"""Планировщик автопостинга на APScheduler 3.x.

Два режима (выбираются через .env):
  • POST_TIMES="09:00,19:00" — публикация по времени (отдельный CronTrigger на каждое);
  • иначе                   — каждые POST_INTERVAL_HOURS часов (IntervalTrigger).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import Settings

logger = logging.getLogger(__name__)

JOB_ID = "autopost"
MISFIRE_GRACE_SECONDS = 600  # бот был выключен — догоняем запуск в течение 10 минут


def describe_schedule(settings: Settings) -> str:
    """Человекочитаемое описание расписания для логов."""
    if settings.post_times:
        times = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in settings.post_times)
        return f"ежедневно в {times} ({settings.timezone})"
    return f"каждые {settings.post_interval_hours:g} ч ({settings.timezone})"


def create_scheduler(
    settings: Settings, job: Callable[[], Awaitable[None]]
) -> AsyncIOScheduler:
    """Создаёт планировщик с задачей автопостинга.

    job обязан быть async-функцией без аргументов: APScheduler 3.x запускает
    корутины в event loop, а обычные функции — в пуле потоков (и корутина
    там просто не выполнится).
    """
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    common = dict(
        func=job,
        replace_existing=True,
        coalesce=True,           # пропущенные запуски не дублируются
        max_instances=1,         # не генерируем два поста одновременно
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )

    if settings.post_times:
        for hour, minute in settings.post_times:
            scheduler.add_job(
                trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
                id=f"{JOB_ID}-{hour:02d}{minute:02d}",
                **common,
            )
    else:
        scheduler.add_job(
            trigger=IntervalTrigger(hours=settings.post_interval_hours, timezone=settings.timezone),
            id=JOB_ID,
            **common,
        )

    logger.info("Планировщик настроен: %s", describe_schedule(settings))
    return scheduler
