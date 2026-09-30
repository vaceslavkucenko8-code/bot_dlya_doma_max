"""Клиент официального MAX Bot API."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _ca_bundle() -> str | bool:
    settings = get_settings()
    if settings.max_ca_bundle:
        return settings.max_ca_bundle
    bundled = Path(__file__).resolve().parents[2] / "certs" / "russian_trusted_root_ca.pem"
    return str(bundled) if bundled.is_file() else True


class MaxApiClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.max_api_base_url.rstrip("/")
        self._token = settings.max_bot_token
        self._timeout = settings.max_api_timeout_seconds
        self._max_retries = settings.max_api_max_retries
        self._verify = _ca_bundle()

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._token, "Accept": "application/json"}

    def get_me(self) -> dict[str, Any]:
        if not self._token:
            raise RuntimeError("MAX_BOT_TOKEN не задан")
        response = httpx.get(
            f"{self._base_url}/me", headers=self._headers(), timeout=self._timeout, verify=self._verify
        )
        response.raise_for_status()
        return response.json()

    def get_subscriptions(self) -> dict[str, Any]:
        if not self._token:
            raise RuntimeError("MAX_BOT_TOKEN не задан")
        response = httpx.get(
            f"{self._base_url}/subscriptions",
            headers=self._headers(), timeout=self._timeout, verify=self._verify,
        )
        response.raise_for_status()
        return response.json()

    def get_updates(self, *, marker: int | None, timeout: int = 30) -> dict[str, Any]:
        if not self._token:
            raise RuntimeError("MAX_BOT_TOKEN не задан")
        params: dict[str, Any] = {
            "limit": 100,
            "timeout": timeout,
            "types": "bot_started,message_created,message_callback",
        }
        if marker is not None:
            params["marker"] = marker
        response = httpx.get(
            f"{self._base_url}/updates", headers=self._headers(), params=params,
            timeout=max(self._timeout, timeout + 5), verify=self._verify,
        )
        response.raise_for_status()
        return response.json()

    def send_message(
        self,
        max_user_id: str,
        text: str,
        *,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> bool:
        """Отправляет сообщение пользователю; сбой доставки не роняет сервис."""
        if not self._token:
            logger.warning("MAX_BOT_TOKEN не задан — уведомление для %s не отправлено", max_user_id)
            return False

        payload: dict[str, Any] = {"text": text[:4000]}
        if buttons:
            payload["attachments"] = [
                {"type": "inline_keyboard", "payload": {"buttons": buttons}}
            ]

        backoff_seconds = 1.0
        for attempt in range(1, self._max_retries + 1):
            try:
                response = httpx.post(
                    f"{self._base_url}/messages", headers=self._headers(),
                    params={"user_id": max_user_id}, json=payload,
                    timeout=self._timeout, verify=self._verify,
                )
                if response.status_code < 300:
                    return True
                if response.status_code < 500 and response.status_code != 429:
                    logger.error(
                        "MAX API отказал (status=%s) для user=%s: %s",
                        response.status_code, max_user_id, response.text[:500],
                    )
                    return False
                logger.warning(
                    "MAX API временно недоступен (status=%s, попытка %s/%s)",
                    response.status_code, attempt, self._max_retries,
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                logger.warning("Ошибка MAX API (попытка %s/%s): %s", attempt, self._max_retries, exc)
            if attempt < self._max_retries:
                time.sleep(backoff_seconds)
                backoff_seconds *= 2

        logger.error("Не удалось доставить уведомление user=%s", max_user_id)
        return False


_client: MaxApiClient | None = None


def get_max_client() -> MaxApiClient:
    global _client
    if _client is None:
        _client = MaxApiClient()
    return _client
