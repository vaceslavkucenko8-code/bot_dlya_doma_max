from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import IncidentStatus

if TYPE_CHECKING:
    from app.models.incident import Incident
    from app.models.user import User


class StatusUpdate(Base):
    """Лог истории смены статусов инцидента. Нужен и для SLA-таймера
    (по времени последнего изменения), и для метрик по защите проекта."""

    __tablename__ = "status_updates"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)

    status: Mapped[IncidentStatus] = mapped_column(
        Enum(
            IncidentStatus,
            name="incident_status",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )

    # Автор изменения — сотрудник УК, если он есть как User(role=management).
    # Может быть NULL для автоматических изменений (например, SLA-напоминание).
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Человекочитаемая пометка источника изменения: "management", "bot", "sla_auto".
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default="management")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    incident: Mapped["Incident"] = relationship(back_populates="status_updates")
    changed_by: Mapped["User | None"] = relationship()
