from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.webhook import router as webhook_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.scheduler import create_scheduler

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.api.deps import webhook_secret_problem

    settings = get_settings()
    problem = webhook_secret_problem(settings.webhook_shared_secret, dev=settings.is_dev_environment)
    if problem:
        # Не роняем процесс (health и веб-часть должны работать), но webhook
        # закрыт, и это видно сразу при старте, а не по первому 503 от бота.
        logger.error("ВНИМАНИЕ: %s — все /webhook/* будут отвечать 503, пока секрет не задан.", problem)
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("SLA-планировщик запущен")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


class BodySizeLimitMiddleware:
    """Ограничение размера тела запроса (MAX_REQUEST_BODY_BYTES) до того, как
    FastAPI начнёт разбирать JSON: иначе мегабайтный payload целиком читается
    в память и валидируется, прежде чем его отклонит max_length поля.
    Проверяется заявленный Content-Length, а тело без него (chunked)
    вычитывается с подсчётом байт до передачи приложению."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = get_settings().max_request_body_bytes
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send, limit)
                return

        if declared is not None:
            await self.app(scope, receive, send)
            return

        # Нет Content-Length (chunked): вычитываем тело сами, не больше лимита,
        # и отдаём приложению уже буферизованным. Бросать исключение из
        # receive нельзя — FastAPI превращает его в 400 вместо 413.
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            received += len(body)
            if received > limit:
                await self._reject(send, limit)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        buffered = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return buffered
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send, limit: int) -> None:
        body = json.dumps(
            {"ok": False, "detail": f"Слишком большой запрос: не больше {limit // 1024} КБ."},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


app = FastAPI(title="ДомРадар backend", lifespan=lifespan)
app.add_middleware(BodySizeLimitMiddleware)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Веб-роутер подключается ПОСЛЕ /health: в нём есть маршрут статики `/{name}`,
# который иначе перехватывал /health и отвечал 403 вне ENVIRONMENT=local
# (ломая docker healthcheck и проверку из README).
from app.api.web import router as web_router  # noqa: E402

app.include_router(web_router)

_FIELD_TITLES = {
    "building_id": "адрес",
    "address": "адрес",
    "text": "описание",
    "location_hint": "уточнение места",
    "category": "категория",
    "incident_id": "номер инцидента",
    "request_id": "идентификатор запроса",
    "id": "номер инцидента",
    "status": "статус",
    "choice": "выбор",
}


def _human_validation_message(exc: RequestValidationError) -> str:
    for error in exc.errors():
        field = next((str(p) for p in reversed(error.get("loc", ())) if isinstance(p, str) and p != "body"), "")
        title = _FIELD_TITLES.get(field, field or "запрос")
        ctx = error.get("ctx") or {}
        kind = error.get("type", "")
        if kind == "string_too_long":
            return f"Поле «{title}»: не больше {ctx.get('max_length')} символов."
        if kind == "string_too_short":
            return f"Поле «{title}»: не меньше {ctx.get('min_length')} символов."
        if kind == "missing":
            return f"Заполните поле «{title}»."
        return f"Проверьте поле «{title}»."
    return "Проверьте введённые данные."


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Для веб-панели — понятная строка на русском (интерфейс показывает
    `detail`, если это строка). Для webhook-контракта бота формат 422 не
    меняется — стандартный список ошибок FastAPI."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=422,
            content={"detail": _human_validation_message(exc), "errors": jsonable_encoder(exc.errors())},
        )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Последний рубеж: один упавший обработчик не должен ронять весь процесс
    и не должен возвращать голый 500 без объяснений."""
    logger.exception("Необработанная ошибка при обработке %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": "internal server error, see backend logs"},
    )
