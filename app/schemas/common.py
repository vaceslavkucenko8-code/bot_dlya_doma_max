"""Общие ограничения входных данных для webhook-контракта бота и веб-панели.

Зачем отдельно: одни и те же правила (длины, очистка управляющих символов,
границы числовых ID) должны действовать одинаково для бота и для сайта —
иначе один канал пропускает то, на чём второй падает.

- Управляющие символы (включая NUL) вырезаются: PostgreSQL не принимает NUL
  в TEXT и отвечает ошибкой драйвера, т.е. голым 500.
- Строки обрезаются по краям: текст из одних пробелов не должен становиться
  заявкой.
- ID ограничены диапазоном INTEGER (1..2^31-1): большее число вызывает
  переполнение в драйвере БД вместо понятного 422/404.
"""
from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BeforeValidator, Field

# Всё из C0/C1, кроме перевода строки и табуляции (они легальны в многострочном тексте).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​  ﻿]")

MAX_DB_INT = 2_147_483_647
MAX_TEXT_LENGTH = 4000
MAX_ADDRESS_LENGTH = 200


def clean_multiline(value: Any) -> Any:
    """Текст сообщения: убираем управляющие символы, нормализуем переводы
    строк, срезаем пробелы по краям. Не-строки отдаём как есть — их отклонит
    сама валидация типа."""
    if not isinstance(value, str):
        return value
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_CHARS.sub("", value)
    # Не больше двух пустых строк подряд — защита от «простыней» из переводов строк.
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def clean_line(value: Any) -> Any:
    """Однострочное значение (адрес, подъезд, ID): ещё и схлопываем любые
    пробельные символы в один пробел."""
    if not isinstance(value, str):
        return value
    value = _CONTROL_CHARS.sub("", value)
    return " ".join(value.split())


MultilineText = Annotated[str, BeforeValidator(clean_multiline)]
SingleLine = Annotated[str, BeforeValidator(clean_line)]
DbId = Annotated[int, Field(ge=1, le=MAX_DB_INT)]
