from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, status

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_warned_no_secret = False

# Значения-заглушки из .env.example и документации: если такой «секрет»
# попал в production, его знает любой, кто видел репозиторий.
_PLACEHOLDER_SECRETS = frozenset({"change-me-shared-secret", "change-me", "changeme", "secret", "put-your-secret-here"})
MIN_PRODUCTION_SECRET_LENGTH = 16


def webhook_secret_problem(secret: str, *, dev: bool) -> str | None:
    """Почему этот секрет нельзя использовать (None — можно)."""
    if not secret:
        return None if dev else "WEBHOOK_SHARED_SECRET не задан"
    if dev:
        return None
    if secret.strip().lower() in _PLACEHOLDER_SECRETS:
        return "WEBHOOK_SHARED_SECRET оставлен значением-заглушкой из .env.example"
    if len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
        return f"WEBHOOK_SHARED_SECRET короче {MIN_PRODUCTION_SECRET_LENGTH} символов"
    return None


def verify_webhook_secret(x_webhook_secret: str | None = Header(default=None)) -> None:
    """Защита webhook-эндпоинтов общим секретом (заголовок X-Webhook-Secret).

    Закрыто по умолчанию: вне локальных окружений (ENVIRONMENT=local/test/
    development) webhook без секрета или со слабым/шаблонным секретом не
    принимает НИ ОДНОГО запроса (503) — иначе забытая переменная окружения
    молча открывала бы смену статусов и чтение заявок всем. Локально без
    секрета можно, но с громким предупреждением в лог."""
    global _warned_no_secret
    settings = get_settings()
    secret = settings.webhook_shared_secret

    problem = webhook_secret_problem(secret, dev=settings.is_dev_environment)
    if problem:
        logger.error("Webhook отключён: %s (ENVIRONMENT=%s)", problem, settings.environment)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook disabled: shared secret is not configured on the server",
        )

    if not secret:
        if not _warned_no_secret:
            logger.warning(
                "WEBHOOK_SHARED_SECRET не задан — webhook-эндпоинты открыты без проверки секрета "
                "(допустимо только при ENVIRONMENT=%s).",
                settings.environment,
            )
            _warned_no_secret = True
        return

    # Сравнение за постоянное время: обычное != позволяет подбирать секрет
    # посимвольно по времени ответа.
    provided = (x_webhook_secret or "").encode("utf-8")
    if not hmac.compare_digest(provided, secret.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook secret")
