import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.building import Building

# Тесты дедупликации не требуют Postgres-специфичных фич — SQLite in-memory
# достаточен и не требует поднятого контейнера БД для `pytest`.
_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db() -> Session:
    Base.metadata.create_all(_engine)
    SessionLocal = sessionmaker(bind=_engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(_engine)


@pytest.fixture()
def building(db: Session) -> Building:
    b = Building(address="г. Тестоград, ул. Примерная, д. 1")
    db.add(b)
    db.commit()
    return b


@pytest.fixture(autouse=True)
def _test_environment(monkeypatch: pytest.MonkeyPatch):
    """По умолчанию ENVIRONMENT=production, а там webhook без сильного секрета
    закрыт (503). Тесты работают в явном тестовом окружении; тесты, которым
    нужен production, переопределяют переменную сами."""
    from app.core.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "test")
    # Локальный .env может содержать реальный токен, но тесты никогда не
    # должны обращаться к живому MAX API.
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
