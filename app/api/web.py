"""Local web adapter over the bot's models. Not a public authentication layer."""
import hashlib
import json
from datetime import timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.webhook import _notify_urgent_incident_background, apply_status_update
from app.core.config import get_settings
from app.db.session import get_db
from app.models import Building, Incident, Report, UserRole, WebReceipt
from app.schemas.common import MAX_ADDRESS_LENGTH, MAX_DB_INT, MAX_TEXT_LENGTH, DbId, MultilineText, SingleLine
from app.schemas.webhook import StatusUpdateEvent
from app.services.classifier import _CATEGORY_LABELS, category_label, classify, find_incident_match, is_urgent
from app.services.locks import incident_dedup_lock
from app.services.repository import get_or_create_building, get_or_create_user, normalize_address

STATUS = {'new': 'open', 'in_progress': 'in_progress', 'resolved': 'closed'}
WEB = Path(__file__).resolve().parents[2] / 'web'


def local_only(request: Request):
    if get_settings().environment != 'local':
        raise HTTPException(403, 'Веб-панель пока доступна только в локальном режиме.')
    if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(403, 'Доступ только с этого компьютера.')
    host = request.headers.get('host', '').split(':')[0]
    if host not in ('127.0.0.1', 'localhost', 'testserver'):
        raise HTTPException(403, 'Недопустимый адрес сервера.')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Запрос с другого сайта отклонён.')


router = APIRouter(dependencies=[Depends(local_only)])


def stamp(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()


def row(i):
    return dict(id=str(i.id), building_id=i.building.address.lower().replace('ё', 'е'),
                category=i.category, description=i.description.split('\n---\n')[0],
                status=STATUS[i.status.value], created_at=stamp(i.created_at), reports_count=len(i.reports))


class Draft(BaseModel):
    building_id: SingleLine = Field(min_length=2, max_length=MAX_ADDRESS_LENGTH)
    text: MultilineText = Field(min_length=5, max_length=MAX_TEXT_LENGTH)
    location_hint: SingleLine = Field(default='', max_length=100)
    category: SingleLine | None = Field(default=None, max_length=100)
    choice: Literal['confirm', 'new'] | None = None
    incident_id: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    request_id: UUID | None = None


def resolve_category(data: Draft) -> str:
    inferred = classify(data.text)
    category = inferred if is_urgent(inferred) else data.category or inferred
    if category not in _CATEGORY_LABELS:
        raise HTTPException(422, 'Неизвестная категория.')
    return category


def decision(data: Draft, db: Session):
    category = resolve_category(data)
    if category == 'other' and not data.category:
        return dict(action='clarify', category=category, category_title=category_label(category),
                    candidate=None, incident_id=None, urgent=False)
    building = db.scalar(select(Building).where(Building.address == normalize_address(data.building_id)))
    match = find_incident_match(
        building.id, category, data.text, location_hint=data.location_hint, db=db
    ) if building else None
    candidate = db.get(Incident, match.incident_id) if match and match.incident_id else None
    return dict(action=match.action if match else 'create', category=category,
                category_title=category_label(category), candidate=row(candidate) if candidate else None,
                incident_id=str(candidate.id) if candidate else None, urgent=is_urgent(category),
                similarity=round(match.similarity, 1) if match else 0.0)


@router.get('/api/meta')
def meta():
    return {'categories': [{'id': key, 'title': value} for key, value in _CATEGORY_LABELS.items()], 'mode': 'local'}


@router.get('/api/incidents')
def incidents(db: Session = Depends(get_db)):
    return [row(i) for i in db.scalars(select(Incident).order_by(Incident.created_at.desc(), Incident.id.desc())).all()]


@router.get('/api/reports')
def reports(id: int = Query(ge=1, le=MAX_DB_INT), db: Session = Depends(get_db)):
    return [{'id': str(r.id), 'description': r.message_text, 'created_at': stamp(r.submitted_at)}
            for r in db.scalars(select(Report).where(Report.incident_id == id).order_by(Report.id)).all()]


@router.post('/api/analyze')
def analyze(data: Draft, db: Session = Depends(get_db)):
    return decision(data, db)


def _replay(db: Session, request_id: str, digest: str):
    receipt = db.get(WebReceipt, request_id, populate_existing=True)
    if receipt is None:
        return None
    if receipt.fingerprint != digest:
        raise HTTPException(409, 'Повторный запрос содержит другие данные.')
    return {'saved': True, 'incident_id': str(receipt.incident_id)}


@router.post('/api/submit')
def submit(data: Draft, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not data.request_id:
        raise HTTPException(422, 'Отсутствует идентификатор запроса.')
    request_id = str(data.request_id)
    digest = hashlib.sha256(json.dumps(data.model_dump(mode='json', exclude={'request_id'}), sort_keys=True).encode()).hexdigest()
    replay = _replay(db, request_id, digest)
    if replay:
        return replay

    category = resolve_category(data)
    building = get_or_create_building(db, data.building_id)
    db.commit()
    with incident_dedup_lock(db, building.id, category):
        replay = _replay(db, request_id, digest)
        if replay:
            return replay
        result = decision(data, db)
        if result['action'] == 'clarify':
            return result
        if data.choice != 'new' and result['incident_id'] != data.incident_id:
            return {**result, 'changed': True, 'saved': False}
        if result['action'] == 'confirm' and data.choice != 'confirm':
            return {**result, 'saved': False}

        user = get_or_create_user(db, 'local-web-resident')
        text = data.text + (f'\nМесто: {data.location_hint}' if data.location_hint else '')
        incident = db.get(Incident, int(result['incident_id'])) if result['incident_id'] and data.choice != 'new' else None
        if incident is None:
            incident = Incident(building_id=building.id, category=result['category'], description=text)
            db.add(incident)
            db.flush()
        else:
            incident.description += '\n---\n' + text
        incident_id = incident.id
        db.add(Report(incident_id=incident_id, user_id=user.id, message_text=text))
        db.add(WebReceipt(request_id=request_id, incident_id=incident_id, fingerprint=digest))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            replay = _replay(db, request_id, digest)
            if replay:
                return replay
            raise

    if result['urgent']:
        tasks.add_task(_notify_urgent_incident_background, incident_id)
    return {'saved': True, 'incident_id': str(incident_id)}


class StatusBody(BaseModel):
    id: DbId
    status: Literal['open', 'in_progress', 'closed']


@router.post('/api/status')
def status(data: StatusBody, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = get_or_create_user(db, 'local-web-dispatcher')
    user.role = UserRole.MANAGEMENT
    db.commit()
    try:
        # Локальный диспетчер (панель доступна только с этого компьютера)
        # ведёт все дома, поэтому без ограничения по дому.
        response = apply_status_update(
            StatusUpdateEvent(max_user_id=user.max_user_id, incident_id=data.id,
                              new_status={'open': 'new', 'in_progress': 'in_progress', 'closed': 'resolved'}[data.status]),
            tasks, db, user, restrict_to_building=None)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(404, 'Инцидент не найден. Обновите список.') from exc
        raise
    return {'ok': response.ok}


class Home(BaseModel):
    address: SingleLine = Field(max_length=MAX_ADDRESS_LENGTH)


@router.get('/api/profile')
def profile(db: Session = Depends(get_db)):
    user = get_or_create_user(db, 'local-web-resident')
    db.commit()
    return {'address': user.building.address if user.building else '', 'verified': False}


@router.post('/api/profile')
def save_profile(data: Home, db: Session = Depends(get_db)):
    address = data.address
    if address and len(address) < 2:
        raise HTTPException(422, 'Укажите полный адрес.')
    user = get_or_create_user(db, 'local-web-resident')
    user.building_id = get_or_create_building(db, address).id if address else None
    user.address_confirmed = bool(address)
    db.commit()
    db.refresh(user)
    return {'address': user.building.address if user.building else '', 'verified': False}


@router.get('/')
def index():
    return FileResponse(WEB / 'index.html')


@router.get('/{name}')
def asset(name: str):
    if name not in ('app.js', 'style.css', 'favicon.svg'):
        raise HTTPException(404)
    return FileResponse(WEB / name)
