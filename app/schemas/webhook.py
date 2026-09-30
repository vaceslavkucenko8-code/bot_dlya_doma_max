"""Payload-схемы событий, которые присылает MAX-бот (Участник 2) на наш
webhook. Если Участнику 2 нужно будет поменять форму запроса — меняем здесь
и в app/api/webhook.py, остальной код (services/*) не затрагивается.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.enums import IncidentStatus
from app.schemas.common import (
    MAX_ADDRESS_LENGTH,
    MAX_TEXT_LENGTH,
    DbId,
    MultilineText,
    SingleLine,
)

# Идентификатор пользователя MAX — колонка users.max_user_id VARCHAR(100).
MaxUserId = SingleLine
_MAX_USER_ID = dict(min_length=1, max_length=100)


class NewMessageEvent(BaseModel):
    max_user_id: MaxUserId = Field(..., **_MAX_USER_ID, description="Идентификатор пользователя в MAX")
    text: MultilineText = Field(
        ..., min_length=1, max_length=MAX_TEXT_LENGTH, description="Текст сообщения жителя"
    )


class NewPhotoEvent(BaseModel):
    max_user_id: MaxUserId = Field(..., **_MAX_USER_ID)
    photo_url: SingleLine = Field(
        ..., min_length=8, max_length=2048, description="URL, по которому backend скачает фото"
    )
    report_id: DbId | None = Field(
        default=None, description="К какой заявке приложить фото; если не указано — берём последнюю заявку автора"
    )

    @field_validator("photo_url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("photo_url должен начинаться с http:// или https://")
        return value


class AddressConfirmationEvent(BaseModel):
    max_user_id: MaxUserId = Field(..., **_MAX_USER_ID)
    building_address: SingleLine = Field(..., min_length=2, max_length=MAX_ADDRESS_LENGTH)
    # users.entrance — VARCHAR(20); пустую строку считаем «не указан».
    entrance: SingleLine | None = Field(default=None, max_length=20)

    @field_validator("entrance")
    @classmethod
    def _empty_entrance_is_none(cls, value: str | None) -> str | None:
        return value or None


class IncidentJoinConfirmationEvent(BaseModel):
    """Ответ жителя на уточняющий вопрос бота "это та же проблема, что и у
    соседей?" — используется, когда авто-дедупликация не уверена и решение
    нужно подтвердить у пользователя."""

    max_user_id: MaxUserId = Field(..., **_MAX_USER_ID)
    report_id: DbId
    confirmed: bool


class StatusQueryEvent(BaseModel):
    """Житель спрашивает бота о статусе своих заявок (например, командой
    "мои заявки" или аналогичной кнопкой) — не дожидаясь push-уведомления."""

    max_user_id: MaxUserId = Field(..., **_MAX_USER_ID)


class StatusUpdateEvent(BaseModel):
    """УК меняет статус инцидента. Приходит через бота (свой чат УК в MAX),
    отдельного REST API для панели УК не делаем — см. README/решение команды."""

    max_user_id: MaxUserId = Field(..., **_MAX_USER_ID, description="Идентификатор сотрудника УК в MAX")
    incident_id: DbId
    new_status: IncidentStatus
    note: MultilineText | None = Field(default=None, max_length=1000)


class BotReplyResponse(BaseModel):
    """Единый формат ответа backend'а боту — этого достаточно, чтобы бот
    продолжил диалог без дополнительных запросов к нам."""

    ok: bool = True
    needs_clarification: bool = False
    clarification_type: str | None = Field(
        default=None, description="'address' | 'category' | 'incident_join' | None"
    )
    incident_id: int | None = None
    report_id: int | None = None
    is_new_incident: bool | None = None
    category: str | None = None
    # Опасная категория (человек застрял в лифте, запах газа) — бот может
    # выделить reply_text визуально (баннер/эмодзи), но вся суть уже есть в
    # самом тексте, так что поле не обязательно к обработке на стороне бота.
    is_urgent: bool = False
    reply_text: str
