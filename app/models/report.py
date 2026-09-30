from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.incident import Incident
    from app.models.user import User


class Report(Base):
    """Одна заявка (жалоба) от конкретного жителя, привязанная к инциденту."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Путь к файлу фото на диске/в volume (если житель приложил фото).
    photo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    incident: Mapped["Incident"] = relationship(back_populates="reports")
    user: Mapped["User"] = relationship(back_populates="reports")
