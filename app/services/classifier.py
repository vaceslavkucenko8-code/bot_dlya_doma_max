"""Точка интеграции с модулем классификации и дедупликации (Участник 1).

ВАЖНО ДЛЯ УЧАСТНИКА 1: это временная заглушка с согласованным интерфейсом.
Замени реализацию функций `classify` и `find_or_create_incident` на свою —
сигнатуры и семантика возврата должны остаться такими же, чтобы не трогать
вызывающий код в app/api/webhook.py. Функции `category_label` и `is_urgent`
тоже можно расширять/менять (например, подключить к ним словарь категорий
из твоей модели) — вызывающий код зависит только от их сигнатур.

    classify(text: str) -> str
        Возвращает строковую категорию проблемы (например "elevator_out_of_service",
        "heating", "water_leak", "other"). Значение просто сохраняется
        в Incident.category, поэтому набор категорий не захардкожен
        в схеме БД — можно менять/расширять без миграций.

    find_incident_match(building_id, category, text, *, db) -> IncidentMatch
        Ищет совместимый открытый инцидент с учётом дома, гранулярной
        категории, временного окна, локации и текстовой похожести. Возвращает
        attach, confirm или create и сам не изменяет базу.

    find_or_create_incident(building_id, category, text, *, db=None) -> (incident_id, is_new)
        Обратная совместимость для старого вызывающего кода: автоматически
        присоединяет только уверенный attach. Для confirm консервативно
        создаёт отдельный инцидент. Новому коду следует использовать
        find_incident_match и явно спрашивать пользователя.
        Параметр `db` — опциональный SQLAlchemy Session; если не передан,
        функция открывает и коммитит свою собственную сессию. Webhook-код
        передаёт свою сессию, чтобы создание Report и Incident были одной
        транзакцией.

    category_label(category: str) -> str
        Человекочитаемая русская подпись категории — для текстов, которые
        видят жители и УК (чтобы не показывать техническое "elevator_out_of_service").
        Для неизвестной категории (например, добавленной Участником 1 и не
        внесённой в словарь) не падает, а просто заменяет "_" на пробел.

    is_urgent(category: str) -> bool
        Категории, где промедление опасно для жизни/здоровья (человек застрял
        в лифте, запах газа) — для них бот сразу советует звонить в экстренные
        службы, а backend уведомляет УК немедленно, не дожидаясь смены статуса
        или срабатывания SLA-таймера (см. app/api/webhook.py).

ПОЧЕМУ АЛГОРИТМ КОНСЕРВАТИВНЫЙ:
на коротких перефразированных русских жалобах простое текстовое сравнение
ненадёжно. Гранулярная категория, временное окно и локация сначала отсекают
несовместимые случаи. Почти идентичный текст или та же точная локация дают
attach; остальные совместимые случаи дают confirm и требуют ответа жителя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.enums import IncidentStatus
from app.models.incident import Incident

# Правила проверяются по порядку; первое полностью совпавшее (все слова из
# хотя бы одной группы встречаются в тексте) правило определяет категорию.
# Порядок важен: более специфичные правила (например, "человек внутри лифта",
# "запах газа") должны стоять раньше общих ("лифт не работает", "нет света").
_CATEGORY_RULES: list[tuple[str, list[tuple[str, ...]]]] = [
    ("elevator_stuck_person", [("лифт", "застря"), ("лифт", "внутри")]),
    ("gas_leak", [("запах", "газ"), ("пахнет", "газ"), ("газом",)]),
    ("elevator_noise", [("лифт", "шум"), ("лифт", "скрип"), ("лифт", "гремит")]),
    ("elevator_out_of_service", [("лифт",)]),
    # "холодно" одно слово слишком общее — пересекается с "холодная вода"
    # (no_water), поэтому требуем его рядом с "кварти"/"дома", а не само по себе.
    ("heating", [("отоплен",), ("батаре",), ("холодно", "кварти"), ("холодно", "дома")]),
    ("water_leak", [("потоп",), ("затоп",), ("залив",), ("залил",), ("прорв",)]),
    ("no_water", [("нет", "вод"), ("пропал", "вод"), ("отключ", "вод")]),
    ("sewage", [("канализац",), ("засор",)]),
    # Именно перегоревшая лампочка/темнота на лестнице — отдельная (и более
    # частая) проблема, чем отключение электричества во всей квартире/доме.
    # Нарочно НЕ используем голое "свет"+"подъезд" — оно совпадает с фразами
    # вида "отключился свет во всём подъезде" (это уже electricity).
    ("common_area_lighting", [("лампочк", "подъезд"), ("лампочк", "лестни"), ("темно", "лестни")]),
    ("electricity", [("свет",), ("электричеств",), ("электрич",)]),
    ("intercom", [("домофон",)]),
    ("roof_leak", [("крыш",), ("капает", "потолк"), ("чердак",)]),
    ("garbage", [("мусор",), ("контейнер",), ("помойк",)]),
    ("pests", [("таракан",), ("грызун",), ("крыс",)]),
    ("parking_barrier", [("шлагбаум",)]),
    ("playground", [("площадк",), ("качел",)]),
    ("entrance_cleanliness", [("подъезд", "грязно"), ("подъезд", "убира")]),
]

# Человекочитаемые подписи — используются в текстах для жителей и УК
# (app/api/webhook.py, app/services/notifications.py), чтобы никто не видел
# внутренний технический слаг категории.
_CATEGORY_LABELS: dict[str, str] = {
    "elevator_stuck_person": "человек застрял в лифте",
    "gas_leak": "запах газа",
    "elevator_noise": "лифт шумит",
    "elevator_out_of_service": "лифт не работает",
    "heating": "проблемы с отоплением",
    "water_leak": "потоп/протечка",
    "no_water": "нет воды",
    "sewage": "засор канализации",
    "common_area_lighting": "не горит свет в подъезде",
    "electricity": "нет электричества",
    "intercom": "домофон не работает",
    "roof_leak": "течёт крыша",
    "garbage": "проблемы с вывозом мусора",
    "pests": "тараканы/грызуны",
    "parking_barrier": "не работает шлагбаум",
    "playground": "повреждена детская площадка",
    "entrance_cleanliness": "грязно в подъезде",
    "other": "другое",
}

# Категории, где промедление может стоить здоровья/жизни — см. is_urgent().
_URGENT_CATEGORIES: frozenset[str] = frozenset({"elevator_stuck_person", "gas_leak"})

# Консервативные пороги из алгоритма V6. Автоматически объединяем только
# почти одинаковые тексты либо обращения с одной и той же точной локацией.
HIGH_SIMILARITY = 92
MEDIUM_SIMILARITY = 45

_WINDOW_HOURS: dict[str, int] = {
    "elevator_stuck_person": 24,
    "elevator_noise": 24,
    "elevator_out_of_service": 24,
    "heating": 48,
    "water_leak": 72,
    "no_water": 72,
    "sewage": 72,
    "common_area_lighting": 72,
    "electricity": 72,
    "intercom": 72,
    "roof_leak": 72,
    "garbage": 48,
    "pests": 72,
    "parking_barrier": 24,
    "playground": 72,
    "entrance_cleanliness": 72,
    "other": 24,
}

_ORDINALS = {
    "первом": 1, "первый": 1, "первого": 1,
    "втором": 2, "второй": 2, "второго": 2,
    "третьем": 3, "третий": 3, "третьего": 3,
    "четвертом": 4, "четвертый": 4, "четвертого": 4,
    "пятом": 5, "пятый": 5, "пятого": 5,
}


@dataclass(frozen=True)
class IncidentMatch:
    action: str
    incident_id: int | None
    confidence: float
    similarity: float
    reason: str


def normalize_text(text: str) -> str:
    normalized = text.lower().replace("ё", "е")
    normalized = re.sub(r"[^а-яa-z0-9\s]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def extract_location(text: str | None) -> str | None:
    if not text:
        return None
    normalized = normalize_text(text)
    match = re.search(r"\bподъезд(?:е|а|у|ом)?\s*(?:номер\s*)?(\d+)\b", normalized)
    if not match:
        match = re.search(r"\b(\d+)\s+подъезд(?:е|а|у|ом)?\b", normalized)
    if match:
        return f"entrance_{int(match.group(1))}"
    for word, number in _ORDINALS.items():
        if re.search(rf"\b{word}\s+подъезд(?:е|а|у|ом)?\b", normalized):
            return f"entrance_{number}"
    if "подвал" in normalized:
        return "basement"
    if any(marker in normalized for marker in ("весь дом", "всем доме", "во всем доме", "у всех")):
        return "whole_building"
    return None


def _hours_since(created_at: datetime, now: datetime) -> float:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - created_at).total_seconds() / 3600


def find_incident_match(
    building_id: int,
    category: str,
    text: str,
    *,
    location_hint: str | None = None,
    created_at: datetime | None = None,
    db: Session,
) -> IncidentMatch:
    """Возвращает ATTACH / CONFIRM / CREATE, не изменяя базу данных."""
    now = created_at or datetime.now(timezone.utc)
    window_hours = _WINDOW_HOURS.get(category, 24)
    incoming_location = extract_location(location_hint) or extract_location(text)
    candidates: list[tuple[Incident, float, str | None]] = []

    rows = db.scalars(
        select(Incident).where(
            Incident.building_id == building_id,
            Incident.category == category,
            Incident.status != IncidentStatus.RESOLVED,
        )
    ).all()
    for incident in rows:
        age = _hours_since(incident.created_at, now)
        if age < 0 or age > window_hours:
            continue
        existing_location = extract_location(incident.description)
        if incoming_location and existing_location and incoming_location != existing_location:
            continue
        similarity = float(fuzz.token_set_ratio(normalize_text(text), normalize_text(incident.description)))
        candidates.append((incident, similarity, existing_location))

    if not candidates:
        return IncidentMatch("create", None, 0.95, 0.0, "Нет совместимого открытого инцидента.")

    incident, similarity, existing_location = max(candidates, key=lambda item: item[1])
    same_location = bool(incoming_location and existing_location and incoming_location == existing_location)
    if same_location and similarity >= MEDIUM_SIMILARITY:
        return IncidentMatch("attach", incident.id, 0.95, similarity, "Совпали категория и точная локация.")
    if similarity >= HIGH_SIMILARITY:
        return IncidentMatch("attach", incident.id, similarity / 100, similarity, "Тексты почти идентичны.")
    if similarity >= MEDIUM_SIMILARITY:
        return IncidentMatch("confirm", incident.id, similarity / 100, similarity, "Есть похожий инцидент; требуется подтверждение жителя.")
    # Текущие категории гранулярны (например, шум лифта и застрявший человек
    # являются разными категориями), поэтому совпадение категории выполняет
    # роль совпадения subtype из V6. Даже при слабом тексте не склеиваем
    # автоматически, а показываем кандидата жителю.
    return IncidentMatch("confirm", incident.id, 0.55, similarity, "Совпала гранулярная категория; требуется подтверждение жителя.")


def classify(text: str) -> str:
    """Простая keyword-эвристика. Участник 1 заменит на ML/NLU классификатор."""
    lowered = text.lower()
    for category, keyword_groups in _CATEGORY_RULES:
        for group in keyword_groups:
            if all(keyword in lowered for keyword in group):
                return category
    return "other"


def category_label(category: str) -> str:
    """Человекочитаемая подпись категории. Для неизвестной категории (например,
    если Участник 1 добавит новую и забудет внести её в словарь) не падает,
    а показывает хоть что-то читаемое вместо технического слага."""
    return _CATEGORY_LABELS.get(category, category.replace("_", " "))


def is_urgent(category: str) -> bool:
    return category in _URGENT_CATEGORIES


def find_or_create_incident(
    building_id: int,
    category: str,
    text: str,
    *,
    db: Session | None = None,
) -> tuple[int, bool]:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        match = find_incident_match(building_id, category, text, db=session)
        if match.action == "attach" and match.incident_id is not None:
            best_match = session.get(Incident, match.incident_id)
            assert best_match is not None
            best_match.description = f"{best_match.description}\n---\n{text}"
            session.add(best_match)
            if owns_session:
                session.commit()
            return best_match.id, False

        new_incident = Incident(building_id=building_id, category=category, description=text)
        session.add(new_incident)
        session.flush()  # получаем new_incident.id без полного commit
        if owns_session:
            session.commit()
        return new_incident.id, True
    finally:
        if owns_session:
            session.close()
