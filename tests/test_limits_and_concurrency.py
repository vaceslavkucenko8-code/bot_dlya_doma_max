"""Ограничения входных данных и защита от конкурентных дублей.

Конкурентные тесты работают на файловой SQLite (у каждого потока своё
соединение, как в реальном сервере), а окно гонки искусственно расширено
задержкой между «прочитал кандидатов» и «закоммитил» — без блокировок эти
тесты стабильно получают дубли, с блокировками — никогда.

Те же тесты можно прогнать на настоящем PostgreSQL (проверяются advisory-
блокировки и INSERT ... ON CONFLICT), указав пустую тестовую базу:

    TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/domradar_test pytest tests/
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.api.webhook as webhook_module
import app.services.max_client as max_client_module
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import Incident, IncidentStatus, Report, StatusUpdate, User, UserRole

THREADS = 6


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "")
    get_settings.cache_clear()
    max_client_module._client = None
    pg_url = os.environ.get("TEST_DATABASE_URL")
    if pg_url:
        engine = create_engine(pg_url, pool_size=THREADS * 2)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'race.sqlite3'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    def _db():
        with factory() as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(webhook_module, "SessionLocal", factory)
    yield TestClient(app), factory
    app.dependency_overrides = previous
    get_settings.cache_clear()
    if pg_url:
        Base.metadata.drop_all(engine)
    engine.dispose()


def _slow_dedup(monkeypatch):
    """Расширяет окно гонки: пауза после выбора/создания инцидента, до commit."""
    original = webhook_module.find_incident_match

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.05)
        return result

    monkeypatch.setattr(webhook_module, "find_incident_match", slow)


def _run_parallel(fn, args_list):
    barrier = threading.Barrier(len(args_list))
    results: list = [None] * len(args_list)
    errors: list = []

    def worker(i, args):
        try:
            barrier.wait()
            results[i] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(args_list)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def _confirm(client, user, address="ул. Гонок, д. 1"):
    assert client.post(
        "/webhook/address-confirmation", json={"max_user_id": user, "building_address": address}
    ).status_code == 200


# ---------------------------------------------------------------- лимиты

def test_health_is_public_outside_local_mode(env, monkeypatch):
    client, _ = env
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    assert client.get("/health").status_code == 200
    assert client.get("/api/incidents").status_code == 403


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/webhook/message", {"max_user_id": "u", "text": "   \n\t  "}),
        ("/webhook/message", {"max_user_id": "u", "text": "л" * 4001}),
        ("/webhook/message", {"max_user_id": "", "text": "Не работает лифт"}),
        ("/webhook/message", {"max_user_id": "u" * 101, "text": "Не работает лифт"}),
        ("/webhook/address-confirmation", {"max_user_id": "u", "building_address": " "}),
        ("/webhook/address-confirmation", {"max_user_id": "u", "building_address": "д" * 201}),
        ("/webhook/address-confirmation", {"max_user_id": "u", "building_address": "Дом 1", "entrance": "1" * 21}),
        ("/webhook/status-update", {"max_user_id": "m", "incident_id": 10**20, "new_status": "resolved"}),
        ("/webhook/status-update", {"max_user_id": "m", "incident_id": 0, "new_status": "resolved"}),
        ("/webhook/status-update", {"max_user_id": "m", "incident_id": 1, "new_status": "resolved", "note": "x" * 1001}),
        ("/webhook/incident-join-confirmation", {"max_user_id": "u", "report_id": -1, "confirmed": False}),
        ("/webhook/photo", {"max_user_id": "u", "photo_url": "file:///etc/passwd"}),
        ("/webhook/photo", {"max_user_id": "u", "photo_url": "https://x/" + "a" * 2100}),
    ],
)
def test_webhook_rejects_out_of_range_input(env, path, payload):
    client, _ = env
    assert client.post(path, json=payload).status_code == 422


def test_control_characters_are_stripped(env):
    client, factory = env
    _confirm(client, "u1", "ул. Нулевая,\x00 д. 5")
    body = client.post("/webhook/message", json={"max_user_id": "u1", "text": "  Не работает\x00 лифт\x07  "}).json()
    assert body["ok"] and body["category"] == "elevator_out_of_service"
    with factory() as db:
        assert db.scalar(select(Report.message_text)) == "Не работает лифт"
        assert db.scalar(select(User.building_id)) is not None


def test_request_body_size_limit(env):
    client, _ = env
    huge = {"max_user_id": "u", "text": "лифт " * 20000}
    resp = client.post("/webhook/message", json=huge)
    assert resp.status_code == 413
    assert "Слишком большой" in resp.json()["detail"]

    # Без Content-Length (chunked) лимит тоже работает.
    def chunks():
        yield b'{"max_user_id":"u","text":"'
        for _ in range(100):
            yield b"a" * 1024
        yield b'"}'

    resp = client.post("/webhook/message", content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_web_limits_and_human_errors(env):
    client, _ = env
    base = dict(building_id="Дом 1", text="Не работает лифт", request_id=str(uuid4()))
    resp = client.post("/api/analyze", json={**base, "text": "л" * 4001})
    assert resp.status_code == 422 and "описание" in resp.json()["detail"] and "4000" in resp.json()["detail"]
    assert client.post("/api/analyze", json={**base, "location_hint": "м" * 101}).status_code == 422
    assert client.post("/api/analyze", json={**base, "category": "x" * 101}).status_code == 422
    assert client.post("/api/submit", json={**base, "incident_id": "1; drop"}).status_code == 422
    assert client.post("/api/submit", json={**base, "text": "     \n  "}).status_code == 422
    assert client.get("/api/reports", params={"id": 10**20}).status_code == 422
    assert client.post("/api/status", json={"id": 10**20, "status": "closed"}).status_code == 422
    missing = client.post("/api/status", json={"id": 999, "status": "closed"})
    assert missing.status_code == 404 and "не найден" in missing.json()["detail"]
    assert client.post("/api/profile", json={"address": "д" * 201}).status_code == 422
    # Webhook-контракт бота сохраняет стандартный формат 422 (список ошибок).
    assert isinstance(client.post("/webhook/message", json={"max_user_id": "u", "text": ""}).json()["detail"], list)


def test_web_submit_does_not_overwrite_saved_home(env):
    client, _ = env
    client.post("/api/profile", json={"address": "Мой дом 1"})
    data = dict(building_id="Чужой дом 9", text="Не работает лифт", request_id=str(uuid4()))
    assert client.post("/api/submit", json=data).json()["saved"]
    assert client.get("/api/profile").json()["address"] == "мой дом 1"


def test_photo_download_is_restricted(env, monkeypatch, tmp_path):
    client, _ = env
    _confirm(client, "u1")
    client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает лифт"})

    # Внутренний адрес — даже не пытаемся скачивать.
    called = []
    monkeypatch.setattr(webhook_module, "_open_stream", lambda *a, **k: called.append(1))
    body = client.post("/webhook/photo", json={"max_user_id": "u1", "photo_url": "http://127.0.0.1:5432/x.jpg"}).json()
    assert body["ok"] is False and not called

    class FakeResponse:
        def __init__(self, content_type, size):
            self.headers = {"content-type": content_type}
            self.is_redirect = False
            self._size = size

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            for _ in range(self._size // 1024):
                yield b"x" * 1024

    def fake_stream(content_type, size):
        @contextmanager
        def stream(*args, **kwargs):
            yield FakeResponse(content_type, size)
        return stream

    public = [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(webhook_module.socket, "getaddrinfo", lambda *a, **k: public)
    monkeypatch.setenv("PHOTO_MAX_BYTES", str(64 * 1024))
    monkeypatch.setenv("PHOTOS_STORAGE_DIR", str(tmp_path / "photos"))
    get_settings.cache_clear()

    monkeypatch.setattr(webhook_module, "_open_stream", fake_stream("text/html", 4096))
    assert client.post("/webhook/photo", json={"max_user_id": "u1", "photo_url": "https://cdn.example/a.jpg"}).json()["ok"] is False

    monkeypatch.setattr(webhook_module, "_open_stream", fake_stream("image/jpeg", 128 * 1024))
    assert client.post("/webhook/photo", json={"max_user_id": "u1", "photo_url": "https://cdn.example/a.jpg"}).json()["ok"] is False


def test_photo_saved_with_safe_suffix(env, monkeypatch, tmp_path):
    client, factory = env
    _confirm(client, "u1")
    client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает лифт"})
    monkeypatch.setenv("PHOTOS_STORAGE_DIR", str(tmp_path / "photos"))
    get_settings.cache_clear()
    monkeypatch.setattr(webhook_module.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])

    class Resp:
        headers = {"content-type": "image/png"}
        is_redirect = False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    @contextmanager
    def stream(*a, **k):
        yield Resp()

    monkeypatch.setattr(webhook_module, "_open_stream", stream)
    body = client.post(
        "/webhook/photo", json={"max_user_id": "u1", "photo_url": "https://cdn.example/evil.php?x=../../a"}
    ).json()
    assert body["ok"] is True
    with factory() as db:
        path = db.scalar(select(Report.photo_path))
    assert path.endswith(".png") and "photos" in path


# ------------------------------------------------------- конкурентные дубли

def test_parallel_neighbors_produce_single_incident(env, monkeypatch):
    client, factory = env
    users = [f"n{i}" for i in range(THREADS)]
    for u in users:
        _confirm(client, u)
    _slow_dedup(monkeypatch)

    replies = _run_parallel(
        lambda u: client.post("/webhook/message", json={"max_user_id": u, "text": "Не работает лифт"}).json(),
        [(u,) for u in users],
    )
    assert len({r["incident_id"] for r in replies}) == 1
    assert sum(r["is_new_incident"] for r in replies) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 1
        assert db.scalar(select(func.count()).select_from(Report)) == THREADS
        # Описание дописано всеми, ни одно обновление не потерялось.
        assert db.scalar(select(Incident.description)).count("Не работает лифт") == THREADS


def test_repeated_delivery_of_same_message_is_not_duplicated(env, monkeypatch):
    client, factory = env
    _confirm(client, "u1")
    _slow_dedup(monkeypatch)
    replies = _run_parallel(
        lambda: client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не  работает ЛИФТ"}).json(),
        [()] * THREADS,
    )
    assert len({r["incident_id"] for r in replies}) == 1
    assert sum("уже приняли" in r["reply_text"] for r in replies) == THREADS - 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Report)) == 1

    # Другой текст от того же жителя — это уже новая информация, принимается.
    client.post("/webhook/message", json={"max_user_id": "u1", "text": "Лифт всё ещё не работает, застряли соседи"})
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Report)) == 2


def test_duplicate_window_expires_and_resolved_incident_is_not_reused(env, monkeypatch):
    client, factory = env
    _confirm(client, "u1")
    first = client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает домофон"}).json()
    with factory() as db:
        db.query(Report).update({Report.submitted_at: datetime.now(timezone.utc) - timedelta(hours=1)})
        db.commit()
    second = client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает домофон"}).json()
    assert "уже приняли" not in second["reply_text"] and second["incident_id"] == first["incident_id"]

    with factory() as db:
        db.query(Incident).update({Incident.status: IncidentStatus.RESOLVED})
        db.commit()
    third = client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает домофон"}).json()
    assert third["is_new_incident"] is True


def test_parallel_status_updates_record_one_change(env):
    client, factory = env
    _confirm(client, "r1")
    incident_id = client.post("/webhook/message", json={"max_user_id": "r1", "text": "Течёт крыша"}).json()["incident_id"]
    with factory() as db:
        building_id = db.scalar(select(Incident.building_id).where(Incident.id == incident_id))
        db.add(User(max_user_id="boss", role=UserRole.MANAGEMENT, address_confirmed=True, building_id=building_id))
        db.commit()

    replies = _run_parallel(
        lambda: client.post(
            "/webhook/status-update",
            json={"max_user_id": "boss", "incident_id": incident_id, "new_status": "in_progress"},
        ).json(),
        [()] * THREADS,
    )
    assert sum("обновлён" in r["reply_text"] for r in replies) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StatusUpdate)) == 1


def test_web_double_submit_same_request_saves_once(env):
    client, factory = env
    data = dict(building_id="Дом 3", text="Нет воды в квартире", request_id=str(uuid4()))
    replies = _run_parallel(lambda: client.post("/api/submit", json=data).json(), [()] * THREADS)
    assert all(r["saved"] for r in replies)
    assert len({r["incident_id"] for r in replies}) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Report)) == 1


def test_web_and_bot_in_parallel_share_one_incident(env, monkeypatch):
    client, factory = env
    users = [f"b{i}" for i in range(THREADS // 2)]
    for u in users:
        _confirm(client, u, "Общий дом 1")
    _slow_dedup(monkeypatch)

    def bot(u):
        return client.post("/webhook/message", json={"max_user_id": u, "text": "Не работает лифт"}).json()["incident_id"]

    def web():
        data = dict(building_id="Общий дом 1", text="Лифт не работает с утра", request_id=str(uuid4()))
        review = client.post("/api/analyze", json=data).json()
        data.update(choice="confirm" if review["incident_id"] else None, incident_id=review["incident_id"])
        result = client.post("/api/submit", json={k: v for k, v in data.items() if v is not None}).json()
        # Если пока проверяли, кто-то успел создать инцидент — интерфейс
        # получает changed и отправляет заново к актуальному инциденту.
        if not result.get("saved"):
            data.update(choice="confirm", incident_id=result["incident_id"])
            result = client.post("/api/submit", json=data).json()
        return int(result["incident_id"])

    ids = _run_parallel(lambda kind, *a: bot(*a) if kind == "bot" else web(),
                        [("bot", u) for u in users] + [("web",)] * (THREADS // 2))
    assert len(set(ids)) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 1
        assert db.scalar(select(func.count()).select_from(Report)) == THREADS


def test_join_confirmation_is_owner_only_and_idempotent(env):
    client, factory = env
    for u in ("a", "b"):
        _confirm(client, u)
    client.post("/webhook/message", json={"max_user_id": "a", "text": "Не работает лифт"})
    client.post("/webhook/message", json={"max_user_id": "b", "text": "Лифт стоит второй день"})
    with factory() as db:
        report_b = db.scalar(select(Report.id).join(User).where(User.max_user_id == "b"))

    assert client.post(
        "/webhook/incident-join-confirmation", json={"max_user_id": "a", "report_id": report_b, "confirmed": False}
    ).status_code == 404

    replies = _run_parallel(
        lambda: client.post(
            "/webhook/incident-join-confirmation",
            json={"max_user_id": "b", "report_id": report_b, "confirmed": False},
        ).json(),
        [()] * 4,
    )
    assert len({r["incident_id"] for r in replies}) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 2


def test_parallel_first_contact_does_not_crash(env):
    """Один и тот же новый житель/дом из нескольких запросов сразу — раньше
    второй запрос падал на UNIQUE(max_user_id)/UNIQUE(address) с 500."""
    client, factory = env
    codes = _run_parallel(
        lambda: client.post(
            "/webhook/address-confirmation", json={"max_user_id": "fresh", "building_address": "Новый дом 42"}
        ).status_code,
        [()] * THREADS,
    )
    assert set(codes) == {200}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_sla_check_works_on_sqlite_naive_datetimes(env, monkeypatch):
    from app.services import sla

    client, factory = env
    _confirm(client, "u1")
    client.post("/webhook/message", json={"max_user_id": "u1", "text": "Не работает домофон"})
    sent = []
    monkeypatch.setattr(sla, "notify_sla_breach", lambda db, incident, hours: sent.append(round(hours)))
    with factory() as db:
        db.query(Incident).update({Incident.last_status_changed_at: datetime.now(timezone.utc) - timedelta(hours=30)})
        db.commit()
        assert sla.check_sla_and_remind(db) == 1
        assert sla.check_sla_and_remind(db) == 0  # cooldown тоже работает на naive-датах
    assert sent == [30]
