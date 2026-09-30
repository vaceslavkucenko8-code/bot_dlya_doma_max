"""Импортируем все модели, чтобы они регистрировались в Base.metadata
(нужно для Alembic autogenerate и для create_all в тестах)."""

from app.models.building import Building
from app.models.enums import IncidentStatus, UserRole
from app.models.incident import Incident
from app.models.report import Report
from app.models.status_update import StatusUpdate
from app.models.user import User
from app.models.web_receipt import WebReceipt

__all__ = [
    "Building",
    "IncidentStatus",
    "UserRole",
    "Incident",
    "Report",
    "StatusUpdate",
    "User",
    "WebReceipt",
]
