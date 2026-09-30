"""Базовые автотесты алгоритма классификации + дедупликации (Шаг 6 ТЗ).

Набор построен на реальных формулировках жалоб жителей: для каждой категории
проблемы — минимум два разных перефразирования, которые должны найти общего
кандидата и при необходимости запросить подтверждение, плюс явные проверки, что РАЗНЫЕ проблемы (в том числе внутри
одной бытовой темы "лифт") не объединяются, и что закрытые/чужие инциденты
не подхватывают новые заявки. Когда Участник 1 подставит свою реализацию
classify()/find_or_create_incident() с тем же интерфейсом — этот файл можно
гонять без изменений, чтобы убедиться, что бизнес-правила дедупликации не
нарушены.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.building import Building
from app.models.enums import IncidentStatus
from app.models.incident import Incident
from app.services.classifier import classify, find_incident_match, find_or_create_incident

# (категория, [перефразирование 1, перефразирование 2]) — должны найти кандидата.
PARAPHRASE_CLUSTERS: list[tuple[str, list[str]]] = [
    (
        "elevator_out_of_service",
        [
            "Уже второй день не работает лифт в нашем подъезде",
            "Лифт снова стоит, не работает третий день подряд",
        ],
    ),
    (
        "elevator_stuck_person",
        [
            "Помогите, лифт застрял между этажами, я внутри!",
            "Застряла в лифте, никто не открывает, помогите",
        ],
    ),
    (
        "elevator_noise",
        [
            "Лифт после ремонта жутко шумит по ночам",
            "Что-то скрипит в лифте, очень громко и мешает спать",
        ],
    ),
    (
        "heating",
        [
            "В квартире очень холодно, батареи еле тёплые",
            "Отопление слабое, дома холодно уже неделю",
        ],
    ),
    (
        "water_leak",
        [
            "Затопило подвал, вода стоит до колена",
            "В подвале потоп, всё заливает водой",
        ],
    ),
    (
        "no_water",
        [
            "Второй день нет холодной воды в кране",
            "Пропала вода, кран сухой уже сутки",
        ],
    ),
    (
        "electricity",
        [
            "Отключился свет во всём подъезде",
            "Пропало электричество, весь дом без света",
        ],
    ),
    (
        "intercom",
        [
            "Домофон не работает, не могу попасть в подъезд",
            "Сломался домофон на входе, дверь не открывается",
        ],
    ),
    (
        "roof_leak",
        [
            "Течёт крыша, на потолке пятна воды",
            "После дождя капает с потолка, крыша течёт",
        ],
    ),
    (
        "garbage",
        [
            "Мусорные контейнеры переполнены, никто не вывозит",
            "Возле мусорки просто свалка, давно не убирают",
        ],
    ),
    (
        "parking_barrier",
        [
            "Шлагбаум во дворе не поднимается",
            "Во дворе не работает шлагбаум, машина не может заехать",
        ],
    ),
]

# Дополнительные одиночные примеры (для теста "разные категории — разные инциденты").
SINGLE_EXAMPLES: list[tuple[str, str]] = [
    ("playground", "Во дворе разбита детская площадка, сломаны качели"),
    ("entrance_cleanliness", "В подъезде очень грязно, никто не убирает"),
]


@pytest.mark.parametrize("expected_category,phrases", PARAPHRASE_CLUSTERS)
def test_classify_matches_expected_category(expected_category: str, phrases: list[str]) -> None:
    for phrase in phrases:
        assert classify(phrase) == expected_category


@pytest.mark.parametrize("expected_category,phrases", PARAPHRASE_CLUSTERS)
def test_paraphrases_require_confirmation_or_attach_confidently(
    db: Session, building: Building, expected_category: str, phrases: list[str]
) -> None:
    first_text, second_text = phrases
    category = classify(first_text)

    incident_id_1, is_new_1 = find_or_create_incident(building.id, category, first_text, db=db)
    decision = find_incident_match(building.id, classify(second_text), second_text, db=db)

    assert is_new_1 is True
    assert decision.action in {"attach", "confirm"}
    assert decision.incident_id == incident_id_1


def test_same_category_in_different_entrances_is_not_a_match(db: Session, building: Building) -> None:
    first = "Не работает лифт в подъезде 1"
    incident_id, _ = find_or_create_incident(building.id, classify(first), first, db=db)

    decision = find_incident_match(
        building.id,
        classify("Лифт сломан во втором подъезде"),
        "Лифт сломан во втором подъезде",
        db=db,
    )

    assert decision.action == "create"
    assert decision.incident_id is None
    assert db.get(Incident, incident_id) is not None


def test_different_categories_create_separate_incidents(db: Session, building: Building) -> None:
    incident_ids: set[int] = set()
    categories: set[str] = set()

    all_examples = [phrases[0] for _, phrases in PARAPHRASE_CLUSTERS] + [text for _, text in SINGLE_EXAMPLES]
    for text in all_examples:
        category = classify(text)
        incident_id, is_new = find_or_create_incident(building.id, category, text, db=db)
        assert is_new is True, f"'{text}' должен был создать новый инцидент, а не присоединиться к существующему"
        incident_ids.add(incident_id)
        categories.add(category)

    assert len(incident_ids) == len(all_examples)
    assert len(categories) == len(all_examples)


def test_elevator_subcategories_are_distinct(db: Session, building: Building) -> None:
    """Все три фразы — про лифт, но это разные по сути проблемы (авария с
    человеком внутри vs простой vs шум) — не должны объединяться."""
    out_of_service_text = "Лифт не работает уже третий день"
    stuck_person_text = "В лифте застрял человек, он внутри и не может выйти"
    noise_text = "Лифт гремит и шумит на весь подъезд"

    ids = set()
    categories = set()
    for text in (out_of_service_text, stuck_person_text, noise_text):
        category = classify(text)
        incident_id, is_new = find_or_create_incident(building.id, category, text, db=db)
        assert is_new is True
        ids.add(incident_id)
        categories.add(category)

    assert len(ids) == 3
    assert len(categories) == 3


def test_resolved_incident_does_not_absorb_new_report(db: Session, building: Building) -> None:
    text = "Не работает лифт в подъезде"
    category = classify(text)

    incident_id, _ = find_or_create_incident(building.id, category, text, db=db)
    incident = db.get(Incident, incident_id)
    incident.status = IncidentStatus.RESOLVED
    db.add(incident)
    db.commit()

    new_text = "Лифт снова не работает"
    new_incident_id, is_new = find_or_create_incident(building.id, classify(new_text), new_text, db=db)

    assert is_new is True
    assert new_incident_id != incident_id


def test_same_problem_in_different_buildings_creates_separate_incidents(db: Session, building: Building) -> None:
    other_building = Building(address="г. Тестоград, ул. Другая, д. 2")
    db.add(other_building)
    db.commit()

    text = "Не работает лифт в подъезде"
    category = classify(text)

    incident_id_1, is_new_1 = find_or_create_incident(building.id, category, text, db=db)
    incident_id_2, is_new_2 = find_or_create_incident(other_building.id, category, text, db=db)

    assert is_new_1 is True
    assert is_new_2 is True
    assert incident_id_1 != incident_id_2
