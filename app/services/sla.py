from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import IncidentStatus
from app.models.incident import Incident
from app.services.notifications import notify_sla_breach

logger = logging.getLogger(__name__)

# Не слать повторное напоминание чаще, чем раз в этот интервал (равен порогу
# SLA), иначе после превышения порога УК будет получать напоминание на каждый
# тик планировщика.
def _reminder_cooldown() -> timedelta:
    return timedelta(hours=get_settings().sla_threshold_hours)


def _as_utc(value: datetime) -> datetime:
    """SQLite (локальный запуск) возвращает naive-datetime в UTC; без этого
    `now - value` падает с TypeError и SLA-проверка локально не работает."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def check_sla_and_remind(db: Session) -> int:
    """Проверяет все открытые инциденты и шлёт напоминание УК, если инцидент
    не менял статус дольше порога. Возвращает количество отправленных
    напоминаний (для логов/метрик)."""
    settings = get_settings()
    threshold = timedelta(hours=settings.sla_threshold_hours)
    now = datetime.now(timezone.utc)
    cutoff = now - threshold

    open_incidents = db.scalars(
        select(Incident).where(
            Incident.status != IncidentStatus.RESOLVED,
            Incident.last_status_changed_at <= cutoff,
        )
    ).all()

    reminders_sent = 0
    cooldown = _reminder_cooldown()
    for incident in open_incidents:
        if incident.sla_last_reminded_at is not None and (now - _as_utc(incident.sla_last_reminded_at)) < cooldown:
            continue

        hours_open = (now - _as_utc(incident.last_status_changed_at)).total_seconds() / 3600
        try:
            notify_sla_breach(db, incident, hours_open)
        except Exception:  # noqa: BLE001 — одна проблемная нотификация не должна ронять весь тик
            logger.exception("Не удалось отправить SLA-напоминание по инциденту %s", incident.id)
            continue

        incident.sla_last_reminded_at = now
        db.add(incident)
        reminders_sent += 1

    db.commit()
    return reminders_sent
