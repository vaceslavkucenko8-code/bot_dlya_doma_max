from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WebReceipt(Base):
    """Квитанция идемпотентности веб-формы: request_id (UUID из браузера) →
    инцидент. Первичный ключ по request_id — последняя линия защиты от
    дублей, даже если два одинаковых запроса пришли в разные процессы."""

    __tablename__ = "web_receipts"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
