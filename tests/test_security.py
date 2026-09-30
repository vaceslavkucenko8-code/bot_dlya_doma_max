"""Закрытие webhook, загрузки фото и проверки владельца обращения.

Каждый тест фиксирует конкретную дыру, найденную в предыдущей версии:
открытый webhook в production, секрет-заглушка, создание пользователей
произвольными запросами, смена статуса чужого дома, «переезд» сотрудника УК,
файл-не-изображение под видом image/jpeg, DNS rebinding, фото к чужой заявке.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.webhook as webhook_module
import app.services.max_client as max_client_module
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import Incident, IncidentStatus, Report, User, UserRole

STRONG = "s3cure-webhook-secret-0123456789"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PUBLIC_IP = "93.184.216.34"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", STRONG)
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("PHOTOS_STORAGE_DIR", str(tmp_path / "photos"))
    get_settings.cache_clear()
    max_client_module._client = None
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    def _db():
        with factory() as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(webhook_module, "SessionLocal", factory)
    client = TestClient(app, headers={"X-Webhook-Secret": STRONG})
    yield client, factory, tmp_path / "photos"
    app.dependency_overrides = previous
    get_settings.cache_clear()
    engine.dispose()


def _confirm(client, user, address="Дом А"):
    assert client.post("/webhook/address-confirmation", json={"max_user_id": user, "building_address": address}).status_code == 200


def _make_management(factory, max_user_id, address):
    with factory() as db:
        from app.models import Building

        building_id = db.scalar(select(Building.id).where(Building.address == address.lower()))
        db.add(User(max_user_id=max_user_id, role=UserRole.MANAGEMENT, address_confirmed=True, building_id=building_id))
        db.commit()


def _users(factory):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(User))


# ------------------------------------------------------------------ webhook

@pytest.mark.parametrize("secret", ["", "change-me-shared-secret", "short"])
def test_webhook_closed_in_production_without_strong_secret(env, monkeypatch, secret):
    client, factory, _ = env
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", secret)
    get_settings.cache_clear()
    resp = client.post("/webhook/status-query", json={"max_user_id": "anon"}, headers={"X-Webhook-Secret": secret})
    assert resp.status_code == 503
    assert _users(factory) == 0
    assert client.get("/health").status_code == 200  # сервис жив, закрыт только webhook


def test_webhook_open_without_secret_only_in_dev(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "")
    monkeypatch.setenv("ENVIRONMENT", "local")
    get_settings.cache_clear()
    assert client.post("/webhook/status-query", json={"max_user_id": "u"}).status_code == 200


def test_wrong_or_missing_secret_rejected(env):
    client, factory, _ = env
    for headers in ({"X-Webhook-Secret": "wrong"}, {"X-Webhook-Secret": STRONG[:-1]}, {"X-Webhook-Secret": ""}):
        assert client.post("/webhook/message", json={"max_user_id": "u", "text": "Лифт"}, headers=headers).status_code == 401
    assert _users(factory) == 0


def test_unknown_users_do_not_create_records(env):
    client, factory, _ = env
    assert client.post("/webhook/status-query", json={"max_user_id": "ghost1"}).json()["needs_clarification"]
    assert client.post("/webhook/message", json={"max_user_id": "ghost2", "text": "Не работает лифт"}).json()["needs_clarification"]
    assert client.post("/webhook/photo", json={"max_user_id": "ghost3", "photo_url": "https://cdn.example/a.jpg"}).status_code == 404
    assert client.post("/webhook/incident-join-confirmation", json={"max_user_id": "ghost4", "report_id": 1, "confirmed": False}).status_code == 404
    assert client.post("/webhook/status-update", json={"max_user_id": "ghost5", "incident_id": 1, "new_status": "resolved"}).status_code == 403
    assert _users(factory) == 0


# ------------------------------------------------- владелец обращения / УК

def test_management_can_change_only_own_building(env):
    client, factory, _ = env
    _confirm(client, "res-a", "Дом А")
    _confirm(client, "res-b", "Дом Б")
    incident_a = client.post("/webhook/message", json={"max_user_id": "res-a", "text": "Течёт крыша"}).json()["incident_id"]
    incident_b = client.post("/webhook/message", json={"max_user_id": "res-b", "text": "Течёт крыша"}).json()["incident_id"]
    _make_management(factory, "uk-b", "Дом Б")

    foreign = client.post("/webhook/status-update", json={"max_user_id": "uk-b", "incident_id": incident_a, "new_status": "resolved"})
    assert foreign.status_code == 404
    own = client.post("/webhook/status-update", json={"max_user_id": "uk-b", "incident_id": incident_b, "new_status": "resolved"})
    assert own.status_code == 200
    with factory() as db:
        assert db.get(Incident, incident_a).status == IncidentStatus.NEW
        assert db.get(Incident, incident_b).status == IncidentStatus.RESOLVED


def test_management_without_building_cannot_change_status(env):
    client, factory, _ = env
    _confirm(client, "res-a")
    incident = client.post("/webhook/message", json={"max_user_id": "res-a", "text": "Течёт крыша"}).json()["incident_id"]
    with factory() as db:
        db.add(User(max_user_id="uk-none", role=UserRole.MANAGEMENT))
        db.commit()
    assert client.post("/webhook/status-update", json={"max_user_id": "uk-none", "incident_id": incident, "new_status": "resolved"}).status_code == 403


def test_management_cannot_relocate_itself_via_bot(env):
    client, factory, _ = env
    _confirm(client, "res-a", "Дом А")
    incident = client.post("/webhook/message", json={"max_user_id": "res-a", "text": "Течёт крыша"}).json()["incident_id"]
    _confirm(client, "res-b", "Дом Б")
    _make_management(factory, "uk-b", "Дом Б")
    resp = client.post("/webhook/address-confirmation", json={"max_user_id": "uk-b", "building_address": "Дом А"})
    assert resp.status_code == 403
    assert client.post("/webhook/status-update", json={"max_user_id": "uk-b", "incident_id": incident, "new_status": "resolved"}).status_code == 404
    # Повторное подтверждение СВОЕГО дома — можно (например, уточнить подъезд).
    assert client.post("/webhook/address-confirmation", json={"max_user_id": "uk-b", "building_address": "дом  б", "entrance": "1"}).status_code == 200


def test_resident_cannot_split_foreign_or_closed_report(env):
    client, factory, _ = env
    _confirm(client, "a")
    _confirm(client, "b")
    client.post("/webhook/message", json={"max_user_id": "a", "text": "Не работает лифт"})
    client.post("/webhook/message", json={"max_user_id": "b", "text": "Лифт стоит"})
    with factory() as db:
        report_b = db.scalar(select(Report.id).join(User).where(User.max_user_id == "b"))
    assert client.post("/webhook/incident-join-confirmation", json={"max_user_id": "a", "report_id": report_b, "confirmed": False}).status_code == 404
    assert client.post("/webhook/incident-join-confirmation", json={"max_user_id": "a", "report_id": report_b, "confirmed": True}).status_code == 404

    with factory() as db:
        db.query(Incident).update({Incident.status: IncidentStatus.RESOLVED})
        db.commit()
    body = client.post("/webhook/incident-join-confirmation", json={"max_user_id": "b", "report_id": report_b, "confirmed": False}).json()
    assert body["ok"] is False and "закрыт" in body["reply_text"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 1


def test_status_query_shows_only_own_incidents(env):
    client, _, _ = env
    _confirm(client, "a")
    _confirm(client, "b")
    client.post("/webhook/message", json={"max_user_id": "a", "text": "Течёт крыша"})
    client.post("/webhook/message", json={"max_user_id": "b", "text": "Нет воды"})
    text = client.post("/webhook/status-query", json={"max_user_id": "a"}).json()["reply_text"]
    assert "крыша" in text and "воды" not in text


def test_local_web_dispatcher_still_manages_all_buildings(env, monkeypatch):
    client, factory, _ = env
    _confirm(client, "res-a", "Дом А")
    incident = client.post("/webhook/message", json={"max_user_id": "res-a", "text": "Течёт крыша"}).json()["incident_id"]
    monkeypatch.setenv("ENVIRONMENT", "local")
    get_settings.cache_clear()
    assert client.post("/api/status", json={"id": incident, "status": "closed"}).json() == {"ok": True}


# ------------------------------------------------------------------ фото

class _FakeResponse:
    def __init__(self, body=PNG, content_type="image/png", status=200, location=None):
        self.headers = {"content-type": content_type}
        if location:
            self.headers["location"] = location
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self._body = body

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield self._body


@pytest.fixture()
def net(monkeypatch):
    """Подменяет DNS и HTTP: dns — имя → IP, responses — очередь ответов,
    calls — фактические запросы (URL, заголовки, extensions)."""
    state = {"dns": {}, "responses": [], "calls": []}

    def getaddrinfo(host, port, *a, **k):
        ip = state["dns"].get(host, PUBLIC_IP)
        return [(2, 1, 6, "", (ip, port))]

    @contextmanager
    def stream(url, headers=None, extensions=None, **kwargs):
        state["calls"].append({"url": url, "headers": headers or {}, "extensions": extensions or {}})
        yield state["responses"].pop(0) if state["responses"] else _FakeResponse()

    monkeypatch.setattr(webhook_module.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(webhook_module, "_open_stream", stream)
    return state


def _report_for(client, user="u1"):
    _confirm(client, user)
    client.post("/webhook/message", json={"max_user_id": user, "text": "Не работает лифт"})


def _photo(client, url="https://cdn.example/a.jpg", user="u1", **extra):
    return client.post("/webhook/photo", json={"max_user_id": user, "photo_url": url, **extra})


def test_photo_connects_to_verified_ip_not_hostname(env, net):
    client, factory, photos = env
    _report_for(client)
    assert _photo(client).json()["ok"] is True
    call = net["calls"][0]
    assert call["url"] == f"https://{PUBLIC_IP}/a.jpg"
    assert call["headers"]["Host"] == "cdn.example"
    assert call["extensions"] == {"sni_hostname": "cdn.example"}
    with factory() as db:
        path = db.scalar(select(Report.photo_path))
    assert path.endswith(".png") and list(photos.iterdir()) == [photos / path.split("/")[-1]]


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "192.168.1.1"])
def test_photo_private_addresses_blocked(env, net, ip):
    client, _, _ = env
    _report_for(client)
    net["dns"]["cdn.example"] = ip
    assert _photo(client).json()["ok"] is False
    assert net["calls"] == []


def test_photo_redirect_to_internal_address_blocked(env, net):
    client, _, _ = env
    _report_for(client)
    net["dns"]["internal.example"] = "10.1.2.3"
    net["responses"].append(_FakeResponse(status=302, location="http://internal.example/secret"))
    assert _photo(client).json()["ok"] is False
    assert len(net["calls"]) == 1


@pytest.mark.parametrize(
    "body,ctype",
    [
        (b'<?php system($_GET["c"]); ?>', "image/jpeg"),
        (b"<svg onload=alert(1)>", "image/svg+xml"),
        (b"MZ\x90\x00", "image/png"),
        (PNG, "text/html"),
        (b"", "image/png"),
    ],
)
def test_photo_content_must_be_real_image(env, net, body, ctype):
    client, factory, photos = env
    _report_for(client)
    net["responses"].append(_FakeResponse(body=body, content_type=ctype))
    assert _photo(client).json()["ok"] is False
    with factory() as db:
        assert db.scalar(select(Report.photo_path)) is None
    assert not photos.exists() or not list(photos.iterdir())


def test_photo_suffix_from_content_not_url(env, net):
    client, factory, _ = env
    _report_for(client)
    net["responses"].append(_FakeResponse(body=JPEG, content_type="image/png"))
    assert _photo(client, url="https://cdn.example/shell.php").json()["ok"] is True
    with factory() as db:
        assert db.scalar(select(Report.photo_path)).endswith(".jpg")


def test_photo_allowed_hosts(env, net, monkeypatch):
    client, _, _ = env
    _report_for(client)
    monkeypatch.setenv("PHOTO_ALLOWED_HOSTS", "cdn.max.ru, .oneme.ru")
    get_settings.cache_clear()
    assert _photo(client, url="https://evil.example/a.jpg").json()["ok"] is False
    assert _photo(client, url="https://cdn.max.ru.evil.example/a.jpg").json()["ok"] is False
    assert _photo(client, url="https://i.cdn.max.ru/a.jpg").json()["ok"] is True
    assert _photo(client, url="https://img.oneme.ru/a.jpg").json()["ok"] is True
    assert [c["headers"]["Host"] for c in net["calls"]] == ["i.cdn.max.ru", "img.oneme.ru"]


def test_photo_url_with_credentials_rejected(env, net):
    client, _, _ = env
    _report_for(client)
    assert _photo(client, url="https://user:pass@cdn.example/a.jpg").json()["ok"] is False


def test_photo_only_to_own_report(env, net):
    client, factory, _ = env
    _report_for(client, "owner")
    _confirm(client, "intruder")
    with factory() as db:
        report_id = db.scalar(select(Report.id))
    assert _photo(client, user="intruder", report_id=report_id).status_code == 404
    assert _photo(client, user="intruder").status_code == 404  # своих заявок нет
    assert _photo(client, user="owner", report_id=report_id).json()["ok"] is True


def test_photo_replacement_removes_previous_file(env, net):
    client, factory, photos = env
    _report_for(client)
    assert _photo(client).json()["ok"]
    net["responses"].append(_FakeResponse(body=JPEG, content_type="image/jpeg"))
    assert _photo(client).json()["ok"]
    files = list(photos.iterdir())
    with factory() as db:
        assert [str(f) for f in files] == [db.scalar(select(Report.photo_path))]
    assert files[0].suffix == ".jpg" and not any(f.name.endswith(".part") for f in files)
