"""Диалоговый адаптер ДомРадара для событий MAX Long Polling."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from fastapi import BackgroundTasks

from app.api.webhook import (
    handle_address_confirmation,
    handle_incident_join_confirmation,
    handle_new_message,
    handle_status_query,
)
from app.db.session import SessionLocal
from app.schemas.webhook import (
    AddressConfirmationEvent,
    IncidentJoinConfirmationEvent,
    NewMessageEvent,
    StatusQueryEvent,
)
from app.services.max_client import MaxApiClient
from app.services.repository import get_user

logger = logging.getLogger(__name__)

_STATUS_WORDS = {"статус", "мои заявки", "мои обращения", "/status"}
_ENTRANCE_RE = re.compile(r"(?:подъезд|подъезда|п\.)\s*№?\s*([\w-]{1,20})", re.IGNORECASE)


class MaxBotRunner:
    def __init__(self, client: MaxApiClient | None = None) -> None:
        self.client = client or MaxApiClient()
        self.marker: int | None = None
        self._pending_join: dict[str, int] = {}

    @staticmethod
    def _user_id(update: dict[str, Any]) -> str | None:
        message = update.get("message") or {}
        callback = update.get("callback") or {}
        user = update.get("user") or callback.get("user") or message.get("sender") or {}
        value = user.get("user_id")
        return str(value) if value is not None else None

    @staticmethod
    def _message_text(update: dict[str, Any]) -> str:
        body = (update.get("message") or {}).get("body") or {}
        return str(body.get("text") or "").strip()

    @staticmethod
    def _run_tasks(tasks: BackgroundTasks) -> None:
        for task in tasks.tasks:
            try:
                task.func(*task.args, **task.kwargs)
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка фоновой задачи после сообщения MAX")

    def _send(self, user_id: str, text: str, *, report_id: int | None = None) -> None:
        buttons = None
        if report_id is not None:
            buttons = [[
                {"type": "callback", "text": "Да, та же", "payload": f"join:{report_id}:yes"},
                {"type": "callback", "text": "Нет, другая", "payload": f"join:{report_id}:no"},
            ]]
        self.client.send_message(user_id, text, buttons=buttons)

    def _welcome(self, user_id: str) -> None:
        with SessionLocal() as db:
            user = get_user(db, user_id)
            if user is None or not user.address_confirmed:
                text = (
                    "Здравствуйте! Я ДомРадар. Помогу сообщить о проблеме в доме и следить за её статусом.\n\n"
                    "Сначала напишите адрес дома. Подъезд можно указать в той же строке, например: "
                    "Москва, ул. Примерная, д. 1, подъезд 2."
                )
            else:
                text = "ДомРадар на связи. Опишите проблему или напишите «мои заявки»."
        self._send(user_id, text)

    def _save_address(self, user_id: str, text: str) -> None:
        entrance_match = _ENTRANCE_RE.search(text)
        entrance = entrance_match.group(1) if entrance_match else None
        address = _ENTRANCE_RE.sub("", text).strip(" ,.;")
        if len(address) < 5:
            self._send(user_id, "Укажите полный адрес дома: город, улицу и номер дома.")
            return
        with SessionLocal() as db:
            reply = handle_address_confirmation(
                AddressConfirmationEvent(
                    max_user_id=user_id, building_address=address, entrance=entrance
                ),
                db,
            )
        self._send(user_id, reply.reply_text)

    def _handle_text(self, user_id: str, text: str) -> None:
        if not text:
            self._send(user_id, "Напишите адрес, описание проблемы или «мои заявки».")
            return
        with SessionLocal() as db:
            user = get_user(db, user_id)
            known = user is not None and user.address_confirmed and user.building_id is not None
        if not known:
            self._save_address(user_id, text)
            return

        normalized = " ".join(text.lower().replace("ё", "е").split())
        if normalized in _STATUS_WORDS or normalized.startswith("статус "):
            with SessionLocal() as db:
                reply = handle_status_query(StatusQueryEvent(max_user_id=user_id), db)
            self._send(user_id, reply.reply_text)
            return

        pending = self._pending_join.get(user_id)
        if pending is not None and normalized in {"да", "да, та же", "нет", "нет, другая"}:
            self._confirm_join(user_id, pending, normalized.startswith("да"))
            return

        tasks = BackgroundTasks()
        with SessionLocal() as db:
            reply = handle_new_message(NewMessageEvent(max_user_id=user_id, text=text), tasks, db)
        self._run_tasks(tasks)
        if reply.needs_clarification and reply.clarification_type == "incident_join" and reply.report_id:
            self._pending_join[user_id] = reply.report_id
            self._send(user_id, reply.reply_text, report_id=reply.report_id)
        else:
            self._send(user_id, reply.reply_text)

    def _confirm_join(self, user_id: str, report_id: int, confirmed: bool) -> None:
        with SessionLocal() as db:
            reply = handle_incident_join_confirmation(
                IncidentJoinConfirmationEvent(
                    max_user_id=user_id, report_id=report_id, confirmed=confirmed
                ),
                db,
            )
        self._pending_join.pop(user_id, None)
        self._send(user_id, reply.reply_text)

    def _handle_callback(self, update: dict[str, Any], user_id: str) -> None:
        callback = update.get("callback") or {}
        payload = str(callback.get("payload") or "")
        match = re.fullmatch(r"join:(\d+):(yes|no)", payload)
        if not match:
            self._send(user_id, "Эта кнопка уже неактуальна. Опишите проблему ещё раз.")
            return
        self._confirm_join(user_id, int(match.group(1)), match.group(2) == "yes")

    def dispatch(self, update: dict[str, Any]) -> None:
        user_id = self._user_id(update)
        if user_id is None:
            logger.info("Пропущено событие MAX без пользователя: %s", update.get("update_type"))
            return
        kind = update.get("update_type")
        if kind == "bot_started":
            self._welcome(user_id)
        elif kind == "message_created":
            self._handle_text(user_id, self._message_text(update))
        elif kind == "message_callback":
            self._handle_callback(update, user_id)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        bot = self.client.get_me()
        logger.info("MAX-бот @%s подключён", bot.get("username"))
        while not stop.is_set():
            try:
                batch = self.client.get_updates(marker=self.marker, timeout=30)
                for update in batch.get("updates", []):
                    self.dispatch(update)
                next_marker = batch.get("marker")
                if next_marker is not None:
                    self.marker = int(next_marker)
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка Long Polling MAX; повтор через 3 секунды")
                stop.wait(3)


def start_max_bot_thread() -> threading.Thread | None:
    client = MaxApiClient()
    if not client.configured:
        logger.warning("MAX_BOT_TOKEN не задан — MAX-бот не запущен")
        return None
    thread = threading.Thread(target=MaxBotRunner(client).run_forever, name="max-bot", daemon=True)
    thread.start()
    return thread
