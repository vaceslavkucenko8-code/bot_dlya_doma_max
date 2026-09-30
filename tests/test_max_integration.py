from __future__ import annotations

from app.core.config import get_settings
from app.services.max_bot import MaxBotRunner
from app.services.max_client import MaxApiClient


class Response:
    status_code = 200
    text = "ok"

    def __init__(self, data=None):
        self.data = data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


def test_send_message_uses_current_max_contract(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("MAX_API_BASE_URL", "https://platform-api2.max.ru")
    monkeypatch.setenv("MAX_CA_BUNDLE", "")
    get_settings.cache_clear()
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("app.services.max_client.httpx.post", post)
    assert MaxApiClient().send_message("123", "Принято") is True
    assert captured["url"] == "https://platform-api2.max.ru/messages"
    assert captured["headers"]["Authorization"] == "test-token"
    assert captured["params"] == {"user_id": "123"}
    assert captured["json"] == {"text": "Принято"}
    assert "access_token" not in captured["params"]


def test_long_polling_passes_marker_and_event_types(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("MAX_CA_BUNDLE", "")
    get_settings.cache_clear()
    captured = {}

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response({"updates": [], "marker": 43})

    monkeypatch.setattr("app.services.max_client.httpx.get", get)
    data = MaxApiClient().get_updates(marker=42, timeout=1)
    assert data["marker"] == 43
    assert captured["params"]["marker"] == 42
    assert "message_created" in captured["params"]["types"]


def test_max_update_extractors_accept_official_shape():
    update = {
        "update_type": "message_created",
        "message": {"sender": {"user_id": 777}, "body": {"text": "Нет воды"}},
    }
    assert MaxBotRunner._user_id(update) == "777"
    assert MaxBotRunner._message_text(update) == "Нет воды"


def test_callback_user_shape_is_supported():
    update = {
        "update_type": "message_callback",
        "callback": {"user": {"user_id": 888}, "payload": "join:5:yes"},
    }
    assert MaxBotRunner._user_id(update) == "888"
