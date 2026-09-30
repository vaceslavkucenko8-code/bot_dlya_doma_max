"""Человекочитаемые подписи статусов инцидента — используются в текстах,
которые видят жители и УК (webhook-ответы, push-уведомления). Вынесено
отдельно от app/models/enums.py, чтобы модели оставались чистым описанием
схемы данных, а не местом для текстов интерфейса."""

from __future__ import annotations

from app.models.enums import IncidentStatus

STATUS_LABELS: dict[IncidentStatus, str] = {
    IncidentStatus.NEW: "зарегистрирован",
    IncidentStatus.IN_PROGRESS: "в работе",
    IncidentStatus.RESOLVED: "устранён",
}


def status_label(status: IncidentStatus) -> str:
    return STATUS_LABELS.get(status, status.value)
