from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import IncidentStatus

if TYPE_CHECKING:
    from app.models.building import Building
    from app.models.report import Report
    from app.models.status_update import StatusUpdate


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        # Основной путь запроса: поиск кандидатов на дедупликацию по дому,
        # категории и (обычно) не закрытому статусу. Должен работать быстро
        # даже при большом числе инцидентов.
        Index("ix_incidents_building_category_status", "building_id", "category", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"), nullable=False)

    # Категория присваивается классификатором (Участник 1). Строка, а не строгий
    # Enum, чтобы не требовать миграцию БД при добавлении новых категорий.
    category: Mapped[str] = mapped_column(String(100), nullable=False)

    # Объединённый текст всех жалоб по этому инциденту (агрегируется при
    # присоединении новых заявок).
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    status: Mapped[IncidentStatus] = mapped_column(
        Enum(
            IncidentStatus,
            name="incident_status",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=IncidentStatus.NEW.value,
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Денормализованное время последнего изменения статуса — чтобы SLA-таймер
    # мог быстро найти "зависшие" инциденты без join с историей StatusUpdate.
    last_status_changed_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    # Когда последний раз отправляли автонапоминание УК по этому инциденту
    # (чтобы не слать напоминание каждый час после превышения порога).
    sla_last_reminded_at: Mapped[datetime | None] = mapped_column(nullable=True)

    building: Mapped["Building"] = relationship(back_populates="incidents")
    reports: Mapped[list["Report"]] = relationship(back_populates="incident", order_by="Report.submitted_at")
    status_updates: Mapped[list["StatusUpdate"]] = relationship(
        back_populates="incident", order_by="StatusUpdate.created_at"
    )
