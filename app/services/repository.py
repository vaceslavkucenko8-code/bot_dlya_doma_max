"""Мелкие get-or-create хелперы для работы с пользователями и домами —
используются webhook-обработчиком и веб-адаптером.

Устойчивость к гонкам: наивное «SELECT → если нет, INSERT» при двух
одновременных запросах с одним max_user_id/адресом даёт нарушение UNIQUE и
500 у второго. Поэтому вставка идёт через INSERT ... ON CONFLICT DO NOTHING
(поддерживают и PostgreSQL, и SQLite), после чего запись просто читается —
кто бы её ни создал."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.building import Building
from app.models.enums import UserRole
from app.models.user import User

_UPSERT_DIALECTS = {"postgresql": pg_insert, "sqlite": sqlite_insert}


def normalize_address(address: str) -> str:
    # Схлопываем повторяющиеся пробелы и приводим «ё» к «е» — частая причина
    # ложных "разных" адресов при ручном/голосовом вводе.
    return " ".join(address.lower().replace("ё", "е").strip().split())


def _insert_ignore(db: Session, model, conflict_column: str, values: dict) -> None:
    insert = _UPSERT_DIALECTS.get(db.get_bind().dialect.name)
    if insert is None:  # другой диалект — обычная вставка без защиты от гонки
        db.add(model(**values))
        db.flush()
        return
    db.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=[conflict_column]))


def get_user(db: Session, max_user_id: str) -> User | None:
    """Только поиск. Для эндпоинтов, которые проверяют владельца или только
    читают: незнакомый max_user_id не должен создавать записи в БД (иначе
    перебором ID можно бесконечно раздувать таблицу users)."""
    return db.scalar(select(User).where(User.max_user_id == max_user_id))


def get_or_create_user(db: Session, max_user_id: str) -> User:
    user = db.scalar(select(User).where(User.max_user_id == max_user_id))
    if user is not None:
        return user

    _insert_ignore(
        db,
        User,
        "max_user_id",
        {"max_user_id": max_user_id, "role": UserRole.RESIDENT, "address_confirmed": False},
    )
    return db.scalars(select(User).where(User.max_user_id == max_user_id)).one()


def get_or_create_building(db: Session, address: str) -> Building:
    normalized = normalize_address(address)
    if not normalized:
        raise ValueError("пустой адрес дома")
    building = db.scalar(select(Building).where(Building.address == normalized))
    if building is not None:
        return building

    _insert_ignore(db, Building, "address", {"address": normalized})
    return db.scalars(select(Building).where(Building.address == normalized)).one()
