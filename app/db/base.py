from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    # Все Mapped[datetime]-колонки по умолчанию timezone-aware (TIMESTAMPTZ в
    # Postgres) — так они всегда совпадают с тем, что реально создаёт Alembic-
    # миграция, и сравнения с datetime.now(timezone.utc) в SLA-логике корректны.
    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }
