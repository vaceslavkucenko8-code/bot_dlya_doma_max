"""HTTP-level тесты webhook-эндпоинтов через FastAPI TestClient — тот же
контракт, что дёргает MAX-бот, но на SQLite in-memory вместо Postgres/Docker.

Дополняют tests/test_dedup.py (который проверяет только бизнес-логику
дедупликации) сквозной проверкой каждого эндпоинта: онбординг через
подтверждение адреса, дедупликация видна в ответе бота (включая "социальное
доказательство" — сколько соседей уже пожаловалось), ролевой доступ к смене
статуса, safety-уведомление для опасных категорий, самостоятельный запрос
статуса заявок жителем.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.webhook as webhook_module
import app.services.max_client as max_client_module
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(bind=_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db
# Фоновые уведомления (BackgroundTasks) открывают свою сессию через
# SessionLocal напрямую, а не через Depends(get_db) — подменяем и его,
# иначе тесты будут пытаться достучаться до настоящего Postgres.
webhook_module.SessionLocal = _TestSessionLocal


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch):
    """Тесты не должны зависеть от того, лежит ли рядом .env с реальными
    значениями (например, от локального docker-compose запуска) — иначе
    WEBHOOK_SHARED_SECRET неожиданно требует заголовок, а фоновая рассылка
    пытается стучаться в реальный MAX API вместо no-op с пустым токеном."""
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "")
    get_settings.cache_clear()
    max_client_module._client = None

    Base.metadata.create_all(_engine)
    yield
    Base.metadata.drop_all(_engine)
    get_settings.cache_clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _confirm_address(client: TestClient, max_user_id: str, entrance: str = "2") -> None:
    resp = client.post(
        "/webhook/address-confirmation",
        json={
            "max_user_id": max_user_id,
            "building_address": "г. Тестоград, ул. Примерная, д. 1",
            "entrance": entrance,
        },
    )
    assert resp.status_code == 200


def test_message_before_address_confirmed_asks_for_address(client: TestClient) -> None:
    resp = client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает лифт"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is True
    assert body["clarification_type"] == "address"


def test_uncertain_dedup_requires_explicit_confirmation(client: TestClient) -> None:
    _confirm_address(client, "u1")
    r1 = client.post(
        "/webhook/message",
        json={"max_user_id": "u1", "text": "Уже второй день не работает лифт в подъезде"},
    )
    b1 = r1.json()
    assert b1["is_new_incident"] is True
    incident_id = b1["incident_id"]

    _confirm_address(client, "u2")
    r2 = client.post(
        "/webhook/message",
        json={"max_user_id": "u2", "text": "Лифт снова стоит, третий день не работает"},
    )
    b2 = r2.json()
    assert b2["is_new_incident"] is False
    assert b2["incident_id"] == incident_id
    assert b2["needs_clarification"] is True
    assert b2["clarification_type"] == "incident_join"
    assert b2["report_id"] is not None

    confirmation = client.post(
        "/webhook/incident-join-confirmation",
        json={"max_user_id": "u2", "report_id": b2["report_id"], "confirmed": True},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["incident_id"] == incident_id
    assert "добавили" in confirmation.json()["reply_text"]


def test_uncertain_dedup_can_be_rejected_and_is_scoped_to_user(client: TestClient) -> None:
    _confirm_address(client, "u1")
    client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает лифт в подъезде"})
    _confirm_address(client, "u2")
    pending = client.post(
        "/webhook/message",
        json={"max_user_id": "u2", "text": "Лифт снова стоит, второй день не работает"},
    ).json()

    denied = client.post(
        "/webhook/incident-join-confirmation",
        json={"max_user_id": "u1", "report_id": pending["report_id"], "confirmed": False},
    )
    assert denied.status_code == 404

    rejected = client.post(
        "/webhook/incident-join-confirmation",
        json={"max_user_id": "u2", "report_id": pending["report_id"], "confirmed": False},
    )
    assert rejected.status_code == 200
    assert rejected.json()["is_new_incident"] is True
    assert rejected.json()["incident_id"] != pending["incident_id"]


def test_status_update_requires_management_role(client: TestClient) -> None:
    _confirm_address(client, "resident1")
    r = client.post("/webhook/message", json={"max_user_id": "resident1", "text": "Не работает лифт"})
    incident_id = r.json()["incident_id"]

    resp = client.post(
        "/webhook/status-update",
        json={"max_user_id": "resident1", "incident_id": incident_id, "new_status": "in_progress"},
    )
    assert resp.status_code == 403


def test_urgent_category_triggers_safety_notice(client: TestClient) -> None:
    _confirm_address(client, "u1")
    resp = client.post(
        "/webhook/message",
        json={"max_user_id": "u1", "text": "Помогите, лифт застрял между этажами, я внутри!"},
    )
    body = resp.json()
    assert body["is_urgent"] is True
    assert "112" in body["reply_text"]


def test_non_urgent_category_has_no_safety_notice(client: TestClient) -> None:
    _confirm_address(client, "u1")
    resp = client.post("/webhook/message", json={"max_user_id": "u1", "text": "Течёт крыша, пятна на потолке"})
    body = resp.json()
    assert body["is_urgent"] is False
    assert "112" not in body["reply_text"]


def test_status_query_lists_own_incidents(client: TestClient) -> None:
    _confirm_address(client, "u1")
    client.post("/webhook/message", json={"max_user_id": "u1", "text": "Течёт крыша, пятна на потолке"})

    resp = client.post("/webhook/status-query", json={"max_user_id": "u1"})
    body = resp.json()
    assert "№" in body["reply_text"]
    assert "зарегистрирован" in body["reply_text"]


def test_status_query_without_reports_is_friendly(client: TestClient) -> None:
    _confirm_address(client, "u1")
    resp = client.post("/webhook/status-query", json={"max_user_id": "u1"})
    body = resp.json()
    assert "нет поданных заявок" in body["reply_text"]


def test_webhook_secret_enforced_when_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "topsecret")
    get_settings.cache_clear()

    resp = client.post("/webhook/message", json={"max_user_id": "x", "text": "тест"})
    assert resp.status_code == 401

    resp_ok = client.post(
        "/webhook/message",
        json={"max_user_id": "x", "text": "тест"},
        headers={"X-Webhook-Secret": "topsecret"},
    )
    assert resp_ok.status_code == 200
