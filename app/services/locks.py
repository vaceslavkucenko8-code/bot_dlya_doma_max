"""Защита от конкурентных дублей.

Проблема: дедупликация устроена как «прочитать открытые инциденты → если нет,
создать новый». Если два жителя (или бот повторил доставку) пишут об одном
и том же почти одновременно, оба запроса успевают прочитать «открытых нет»
и создают ДВА инцидента — ровно те дубли, ради устранения которых существует
продукт. То же с дописыванием описания (потерянное обновление) и повторной
сменой статуса (двойная запись в историю и двойная рассылка).

Решение — сериализовать критическую секцию по ключу (например, дом+категория):

- внутри одного процесса — полосатый набор RLock (256 штук, память не растёт
  с числом ключей);
- между процессами/репликами на PostgreSQL — транзакционная advisory-блокировка
  `pg_advisory_xact_lock`, которая сама снимается на COMMIT/ROLLBACK.

Контракт использования: всё, что читает и пишет защищаемые данные, включая
`db.commit()`, должно выполняться ВНУТРИ `with critical_section(...)`.
Входить в секцию нужно без незафиксированных записей в сессии: в SQLite
открытая пишущая транзакция, ждущая блокировку, не даст писать её держателю.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

_STRIPES = 256
_process_locks = [threading.RLock() for _ in range(_STRIPES)]


def _key_hash(parts: tuple[object, ...]) -> int:
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


@contextmanager
def critical_section(db: Session, namespace: str, *key: object) -> Iterator[None]:
    digest = _key_hash((namespace, *key))
    process_lock = _process_locks[digest % _STRIPES]
    with process_lock:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": digest})
        yield


def incident_dedup_lock(db: Session, building_id: int, category: str):
    """Одна секция на пару (дом, категория) — ровно та область, в которой
    find_or_create_incident выбирает, присоединять заявку или создавать новую."""
    return critical_section(db, "incident-dedup", building_id, category)
