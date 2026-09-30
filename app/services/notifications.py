"""Логика "кому и что отправить" при смене статуса инцидента, при срочном
(safety-critical) новом инциденте и при срабатывании SLA-таймера. Сама
доставка идёт через MaxApiClient (app/services/max_client.py) — то есть
через инфраструктуру бота Участника 2.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.incident import Incident
from app.models.report import Report
from app.models.user import User
from app.services.classifier import category_label
from app.services.labels import status_label
from app.services.max_client import get_max_client

logger = logging.getLogger(__name__)


def _subscriber_max_ids(db: Session, incident_id: int) -> list[str]:
    """Все пользователи, у которых есть хотя бы одна заявка (Report) на этот
    инцидент — они и есть подписчики на уведомления о его статусе."""
    rows = db.execute(
        select(User.max_user_id)
        .join(Report, Report.user_id == User.id)
        .where(Report.incident_id == incident_id)
        .distinct()
    ).all()
    return [row[0] for row in rows]


def _management_of_building(db: Session, building_id: int) -> list[User]:
    return list(
        db.scalars(select(User).where(User.role == UserRole.MANAGEMENT, User.building_id == building_id)).all()
    )


def notify_status_change(db: Session, incident: Incident) -> None:
    subscriber_ids = _subscriber_max_ids(db, incident.id)
    if not subscriber_ids:
        logger.info("Инцидент %s: нет подписчиков для уведомления", incident.id)
        return

    text = (
        f"Обновление по вашей заявке: «{category_label(incident.category)}» — "
        f"статус изменён на «{status_label(incident.status)}»."
    )

    client = get_max_client()
    for max_user_id in subscriber_ids:
        delivered = client.send_message(max_user_id, text)
        if not delivered:
            logger.warning(
                "Не удалось уведомить пользователя %s об инциденте %s (продолжаем с остальными)",
                max_user_id,
                incident.id,
            )


def notify_urgent_incident(db: Session, incident: Incident) -> None:
    """Опасные категории (человек застрял в лифте, запах газа) не должны
    ждать смены статуса или SLA-таймера — УК уведомляется сразу при создании
    такого инцидента (см. app/services/classifier.py: is_urgent)."""
    management_users = _management_of_building(db, incident.building_id)
    if not management_users:
        logger.warning(
            "Срочный инцидент %s (дом %s, категория %s) создан, но в БД нет "
            "зарегистрированного пользователя УК для этого дома — немедленное "
            "уведомление не отправлено",
            incident.id,
            incident.building_id,
            incident.category,
        )
        return

    text = (
        f"🆘 СРОЧНО: новая заявка «{category_label(incident.category)}» "
        f"(инцидент №{incident.id}). Требует немедленной реакции."
    )

    client = get_max_client()
    for management_user in management_users:
        client.send_message(management_user.max_user_id, text)


def notify_sla_breach(db: Session, incident: Incident, hours_open: float) -> None:
    """Автонапоминание УК: инцидент не менял статус дольше порога."""
    management_users = _management_of_building(db, incident.building_id)

    if not management_users:
        logger.warning(
            "SLA нарушен для инцидента %s (дом %s), но в БД нет зарегистрированного "
            "пользователя УК для этого дома — уведомление не отправлено",
            incident.id,
            incident.building_id,
        )
        return

    text = (
        f"⚠️ SLA: инцидент №{incident.id} («{category_label(incident.category)}») не меняет статус "
        f"уже {hours_open:.0f} ч. Текущий статус: {status_label(incident.status)}."
    )

    client = get_max_client()
    for management_user in management_users:
        client.send_message(management_user.max_user_id, text)
