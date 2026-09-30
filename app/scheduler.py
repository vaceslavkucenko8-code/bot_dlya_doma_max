from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.sla import check_sla_and_remind

logger = logging.getLogger(__name__)


def _run_sla_check_job() -> None:
    """Обёртка для планировщика: одна упавшая проверка не должна убить job
    навсегда (APScheduler по умолчанию просто залогирует и продолжит по
    расписанию, но перехватываем явно, чтобы гарантировать закрытие сессии)."""
    db = SessionLocal()
    try:
        sent = check_sla_and_remind(db)
        if sent:
            logger.info("SLA-проверка: отправлено %s напоминаний", sent)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка при выполнении SLA-проверки")
        db.rollback()
    finally:
        db.close()


def create_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_sla_check_job,
        "interval",
        minutes=settings.sla_check_interval_minutes,
        id="sla_check",
        coalesce=True,
        max_instances=1,
    )
    return scheduler
