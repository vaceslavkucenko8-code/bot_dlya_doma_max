from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.building import Building
    from app.models.report import Report


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Уникальный идентификатор пользователя в MAX.
    max_user_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        server_default=UserRole.RESIDENT.value,
    )

    building_id: Mapped[int | None] = mapped_column(ForeignKey("buildings.id"), nullable=True)
    # Подъезд, подтверждённый пользователем в диалоге с ботом.
    entrance: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Пока житель не подтвердил адрес/подъезд, мы не можем доверять дедупликации по дому.
    address_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    building: Mapped["Building | None"] = relationship(back_populates="users")
    reports: Mapped[list["Report"]] = relationship(back_populates="user")
