from __future__ import annotations

import ipaddress
import logging
import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import verify_webhook_secret
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.enums import IncidentStatus, UserRole
from app.models.incident import Incident
from app.models.report import Report
from app.models.status_update import StatusUpdate
from app.models.user import User
from app.schemas.webhook import (
    AddressConfirmationEvent,
    BotReplyResponse,
    IncidentJoinConfirmationEvent,
    NewMessageEvent,
    NewPhotoEvent,
    StatusQueryEvent,
    StatusUpdateEvent,
)
from app.services.classifier import category_label, classify, find_incident_match, is_urgent
from app.services.labels import status_label
from app.services.locks import critical_section, incident_dedup_lock
from app.services.notifications import notify_status_change, notify_urgent_incident
from app.services.repository import get_or_create_building, get_or_create_user, get_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"], dependencies=[Depends(verify_webhook_secret)])

_EMERGENCY_NOTICE = (
    " ⚠️ Если это опасно для жизни или здоровья — не ждите ответа бота, "
    "сразу звоните 112 (или в аварийную/лифтовую службу вашего дома)."
)


def _pluralize_ru(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: 1 житель, 2 жителя, 5 жителей, 11 жителей, 21 житель."""
    n_mod100 = abs(n) % 100
    if 11 <= n_mod100 <= 14:
        return many
    n_mod10 = n_mod100 % 10
    if n_mod10 == 1:
        return one
    if 2 <= n_mod10 <= 4:
        return few
    return many


def _as_utc(value: datetime) -> datetime:
    """SQLite возвращает naive-datetime (в UTC), PostgreSQL — aware."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _normalized_text(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _recent_duplicate_incident(db: Session, user_id: int, text: str) -> int | None:
    """Инцидент, к которому этот же житель уже отправил ТОТ ЖЕ текст в
    пределах окна DUPLICATE_MESSAGE_WINDOW_SECONDS, если инцидент ещё открыт.
    Вызывается под incident_dedup_lock — одинаковый текст классифицируется в
    одну категорию, значит параллельные повторы сериализуются этой же
    блокировкой и второй увидит уже закоммиченную заявку первого."""
    window = get_settings().duplicate_message_window_seconds
    if window <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    wanted = _normalized_text(text)
    recent = db.execute(
        select(Report.incident_id, Report.message_text, Report.submitted_at)
        .join(Incident, Incident.id == Report.incident_id)
        .where(Report.user_id == user_id, Incident.status != IncidentStatus.RESOLVED)
        .order_by(Report.id.desc())
        .limit(20)
    ).all()
    for incident_id, message_text, submitted_at in recent:
        if submitted_at is not None and _as_utc(submitted_at) < cutoff:
            break
        if _normalized_text(message_text) == wanted:
            return incident_id
    return None


@router.post("/message", response_model=BotReplyResponse)
def handle_new_message(
    event: NewMessageEvent, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> BotReplyResponse:
    # Незнакомому пользователю просто задаём вопрос об адресе — запись в БД
    # появится только при подтверждении адреса.
    user = get_user(db, event.max_user_id)

    if user is None or not user.address_confirmed or user.building_id is None:
        return BotReplyResponse(
            needs_clarification=True,
            clarification_type="address",
            reply_text=(
                "Похоже, вы пишете нам впервые. Прежде чем зарегистрировать заявку, "
                "подскажите, пожалуйста, точный адрес дома и номер подъезда."
            ),
        )

    duplicate_of: int | None = None
    report: Report | None = None
    needs_confirmation = False
    try:
        category = classify(event.text)
        # Вся секция «найти/создать инцидент → добавить заявку → commit» под
        # блокировкой (дом, категория): иначе одновременные жалобы соседей на
        # одно и то же создают несколько инцидентов вместо одного.
        with incident_dedup_lock(db, user.building_id, category):
            duplicate_of = _recent_duplicate_incident(db, user.id, event.text)
            if duplicate_of is None:
                location_hint = f"подъезд {user.entrance}" if user.entrance else None
                match = find_incident_match(
                    user.building_id, category, event.text, location_hint=location_hint, db=db
                )
                needs_confirmation = match.action == "confirm"
                if match.action == "create":
                    incident = Incident(building_id=user.building_id, category=category, description=event.text)
                    db.add(incident)
                    db.flush()
                    is_new = True
                else:
                    incident = db.get(Incident, match.incident_id)
                    if incident is None:
                        raise RuntimeError("dedup candidate disappeared")
                    is_new = False
                    if match.action == "attach":
                        incident.description = f"{incident.description}\n---\n{event.text}"
                        db.add(incident)
                incident_id = incident.id
                report = Report(incident_id=incident_id, user_id=user.id, message_text=event.text)
                db.add(report)
                db.flush()
            db.commit()
    except Exception:  # noqa: BLE001 — сбой классификатора не должен рушить весь сервис
        logger.exception("Ошибка классификации/дедупликации для пользователя %s", event.max_user_id)
        db.rollback()
        return BotReplyResponse(
            ok=False,
            reply_text=(
                "Не получилось обработать сообщение из-за технической ошибки. "
                "Мы уже знаем о проблеме, попробуйте отправить сообщение ещё раз чуть позже."
            ),
        )

    label = category_label(category)
    urgent = is_urgent(category)

    if duplicate_of is not None:
        # Повторная доставка/двойное нажатие: новую заявку не создаём, повторно
        # УК не дёргаем, но совет звонить 112 для опасных категорий оставляем.
        reply_text = (
            f"Эту заявку мы уже приняли — инцидент №{duplicate_of} («{label}»). "
            "Повторно отправлять не нужно, статус можно спросить у меня в любой момент."
        )
        return BotReplyResponse(
            incident_id=duplicate_of,
            is_new_incident=False,
            category=category,
            is_urgent=urgent,
            reply_text=reply_text + (_EMERGENCY_NOTICE if urgent else ""),
        )

    if needs_confirmation:
        assert report is not None
        reply_text = (
            f"Нашли похожий инцидент №{incident_id} («{label}»), но совпадение не точное. "
            "Это та же самая проблема? Ответьте через кнопки «Да, та же» или «Нет, другая»."
        )
        if urgent:
            reply_text += _EMERGENCY_NOTICE
        return BotReplyResponse(
            needs_clarification=True,
            clarification_type="incident_join",
            incident_id=incident_id,
            report_id=report.id,
            is_new_incident=False,
            category=category,
            is_urgent=urgent,
            reply_text=reply_text,
        )

    if is_new:
        reply_text = f"Спасибо! Зарегистрировали заявку «{label}» — инцидент №{incident_id}."
        if urgent:
            # Опасная ситуация — УК уведомляется немедленно, не дожидаясь
            # смены статуса или срабатывания SLA-таймера через сутки.
            background_tasks.add_task(_notify_urgent_incident_background, incident_id)
    else:
        report_count = db.scalar(select(func.count()).select_from(Report).where(Report.incident_id == incident_id))
        neighbors = max((report_count or 1) - 1, 0)
        if neighbors > 0:
            word = _pluralize_ru(neighbors, "сосед уже сообщил", "соседа уже сообщили", "соседей уже сообщили")
            reply_text = (
                f"Спасибо! Такую же проблему («{label}») {neighbors} {word} — присоединили вашу "
                f"заявку к инциденту №{incident_id}, чтобы УК сразу видела масштаб, а не разбирала дубли."
            )
        else:
            reply_text = f"Спасибо! Присоединили вашу заявку к инциденту №{incident_id} («{label}»)."

    if urgent:
        reply_text += _EMERGENCY_NOTICE

    return BotReplyResponse(
        incident_id=incident_id,
        is_new_incident=is_new,
        category=category,
        is_urgent=urgent,
        reply_text=reply_text,
    )


_ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp",
    "image/gif", "image/heic", "image/heif",
}
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}
_MAX_PHOTO_REDIRECTS = 3


class PhotoRejected(ValueError):
    pass


def _sniff_image_suffix(data: bytes) -> str | None:
    """Тип файла по сигнатуре (magic bytes), а не по заголовку сервера:
    Content-Type задаёт тот, кто отдаёт файл, и подделывается одной строкой."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return ".heic"
    return None


def _host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return not allowed or any(host == a or host.endswith("." + a) for a in allowed)


def _resolve_public_address(url: str) -> tuple[str, str, int, str]:
    """Защита от SSRF: только http(s), только разрешённые хосты
    (PHOTO_ALLOWED_HOSTS) и только публичные IP. Возвращает ПРОВЕРЕННЫЙ IP —
    дальше соединяемся именно с ним, иначе между проверкой и запросом DNS
    может вернуть уже внутренний адрес (DNS rebinding)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PhotoRejected("неподдерживаемый URL фото")
    if parts.username or parts.password:
        raise PhotoRejected("URL фото не должен содержать логин/пароль")
    host = parts.hostname
    if not _host_allowed(host, get_settings().photo_allowed_host_list):
        raise PhotoRejected(f"хост {host} не входит в PHOTO_ALLOWED_HOSTS")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise PhotoRejected("некорректный порт в URL фото") from exc
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PhotoRejected(f"не удалось разрешить хост {host}") from exc
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses:
        raise PhotoRejected(f"не удалось разрешить хост {host}")
    for address in addresses:
        if not address.is_global:
            raise PhotoRejected(f"URL фото ведёт на непубличный адрес {address}")
    return parts.scheme, host, port, str(addresses[0])


def _pinned_request(url: str) -> tuple[str, dict[str, str], dict[str, str]]:
    scheme, host, port, ip = _resolve_public_address(url)
    parts = urlsplit(url)
    ip_netloc = f"[{ip}]" if ":" in ip else ip
    default_port = 443 if scheme == "https" else 80
    if port != default_port:
        ip_netloc += f":{port}"
    pinned = urlunsplit((scheme, ip_netloc, parts.path or "/", parts.query, ""))
    headers = {"Host": host if port == default_port else f"{host}:{port}", "Accept": "image/*"}
    # SNI и проверка сертификата — по исходному имени хоста, соединение — по IP.
    extensions = {"sni_hostname": host} if scheme == "https" else {}
    return pinned, headers, extensions


@contextmanager
def _open_stream(url: str, headers: dict[str, str], extensions: dict[str, str]):
    """Единственная точка сетевого запроса за фото (её же подменяют тесты).
    Именно Client.stream: функция httpx.stream() не принимает extensions,
    а без sni_hostname соединение по IP не пройдёт проверку сертификата."""
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        with client.stream("GET", url, headers=headers, extensions=extensions) as response:
            yield response


def _download_photo(url: str, max_bytes: int) -> tuple[bytes, str]:
    """Скачивает фото потоково с лимитом размера, проверкой заголовка и
    сигнатуры файла. Редиректы проходим вручную, проверяя каждый адрес."""
    for _ in range(_MAX_PHOTO_REDIRECTS + 1):
        pinned, headers, extensions = _pinned_request(url)
        with _open_stream(pinned, headers, extensions) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in _ALLOWED_IMAGE_TYPES:
                raise PhotoRejected(f"по ссылке не изображение (content-type={content_type!r})")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise PhotoRejected("фото больше допустимого размера")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise PhotoRejected("фото больше допустимого размера")
                chunks.append(chunk)
            content = b"".join(chunks)
            suffix = _sniff_image_suffix(content)
            if suffix is None:
                raise PhotoRejected("содержимое файла не является изображением JPG/PNG/GIF/WEBP/HEIC")
            return content, suffix
    raise PhotoRejected("слишком много перенаправлений при скачивании фото")


def _store_photo(content: bytes, suffix: str) -> Path:
    """Атомарная запись: сначала во временный файл, потом rename — чтобы при
    сбое посередине не остался обрезанный файл, на который ссылается заявка."""
    storage_dir = Path(get_settings().photos_storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    final_path = storage_dir / f"{uuid.uuid4().hex}{suffix}"
    tmp_path = final_path.with_suffix(final_path.suffix + ".part")
    tmp_path.write_bytes(content)
    tmp_path.chmod(0o640)
    os.replace(tmp_path, final_path)
    return final_path


def _remove_stored_photo(path: str | None) -> None:
    """Удаляет прежнее фото заявки при замене — только внутри каталога фото."""
    if not path:
        return
    storage_dir = Path(get_settings().photos_storage_dir).resolve()
    try:
        candidate = Path(path).resolve()
        if candidate.parent == storage_dir and candidate.is_file():
            candidate.unlink()
    except OSError:
        logger.warning("Не удалось удалить прежнее фото %s", path)


@router.post("/photo", response_model=BotReplyResponse)
def handle_new_photo(event: NewPhotoEvent, db: Session = Depends(get_db)) -> BotReplyResponse:
    # Фото прикладывается только к СВОЕЙ заявке: пользователь должен уже
    # существовать (незнакомый max_user_id не создаёт записей в БД), а заявка
    # — принадлежать ему. Чужой report_id неотличим от несуществующего (404).
    user = get_user(db, event.max_user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="report not found for this user")

    if event.report_id is not None:
        report = db.scalar(select(Report).where(Report.id == event.report_id, Report.user_id == user.id))
    else:
        report = db.scalar(
            select(Report).where(Report.user_id == user.id).order_by(Report.submitted_at.desc(), Report.id.desc()).limit(1)
        )

    if report is None:
        raise HTTPException(status_code=404, detail="report not found for this user")
    report_id, incident_id = report.id, report.incident_id
    db.commit()  # не держим транзакцию БД открытой, пока идёт скачивание по сети

    try:
        content, suffix = _download_photo(event.photo_url, get_settings().photo_max_bytes)
    except PhotoRejected as exc:
        logger.warning("Фото для report_id=%s отклонено: %s", report_id, exc)
        return BotReplyResponse(
            ok=False,
            incident_id=incident_id,
            reply_text=(
                "Не получилось принять фото: нужен файл изображения (JPG, PNG, WEBP, HEIC) "
                "не больше допустимого размера. Заявка уже зарегистрирована без него."
            ),
        )
    except Exception:  # noqa: BLE001 — недоступность внешнего URL не должна рушить webhook
        logger.exception("Не удалось скачать фото для report_id=%s", report_id)
        return BotReplyResponse(
            ok=False,
            incident_id=incident_id,
            reply_text="Не получилось сохранить фото, но заявка уже зарегистрирована без него.",
        )

    new_path: Path | None = None
    try:
        new_path = _store_photo(content, suffix)
        # Две одновременные загрузки к одной заявке: без секции обе запишут
        # файл, а в БД останется ссылка только на один — второй станет «сиротой».
        with critical_section(db, "report-photo", report_id):
            db.refresh(report)
            previous = report.photo_path
            report.photo_path = str(new_path)
            db.commit()
        _remove_stored_photo(previous)
        return BotReplyResponse(incident_id=incident_id, reply_text="Фото получили, приложили к заявке. Спасибо!")
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сохранить фото для report_id=%s", report_id)
        db.rollback()
        if new_path is not None:
            _remove_stored_photo(str(new_path))
        return BotReplyResponse(
            ok=False,
            incident_id=incident_id,
            reply_text="Не получилось сохранить фото, но заявка уже зарегистрирована без него.",
        )


@router.post("/address-confirmation", response_model=BotReplyResponse)
def handle_address_confirmation(event: AddressConfirmationEvent, db: Session = Depends(get_db)) -> BotReplyResponse:
    user = get_or_create_user(db, event.max_user_id)
    building = get_or_create_building(db, event.building_address)

    # Дом сотрудника УК определяет, чьи инциденты он может менять. Если бы
    # сотрудник мог сам «переехать» через бота, проверка владельца в
    # /status-update обходилась бы одним сообщением. Привязку УК к дому
    # меняет администратор в БД, а не бот.
    if user.role == UserRole.MANAGEMENT and user.building_id is not None and user.building_id != building.id:
        db.rollback()
        raise HTTPException(
            status_code=403, detail="management users cannot change their building via the bot"
        )

    user.building_id = building.id
    user.entrance = event.entrance
    user.address_confirmed = True
    db.add(user)
    db.commit()

    return BotReplyResponse(
        reply_text=(
            f"Адрес подтверждён: {building.address}"
            + (f", подъезд {event.entrance}" if event.entrance else "")
            + ". Теперь опишите проблему одним сообщением — если о том же уже писали соседи, "
            "мы объединим заявки, чтобы УК видела не дубли, а реальный масштаб. Статус своих "
            "заявок можно спросить у меня в любой момент."
        )
    )


@router.post("/incident-join-confirmation", response_model=BotReplyResponse)
def handle_incident_join_confirmation(
    event: IncidentJoinConfirmationEvent, db: Session = Depends(get_db)
) -> BotReplyResponse:
    """Завершает сомнительное совпадение после явного ответа жителя."""
    # Житель может отделять только СВОЮ заявку — иначе любой, кто угадал
    # report_id, мог бы растаскивать чужие жалобы по отдельным инцидентам.
    # Чужая заявка неотличима от несуществующей (404), незнакомый
    # max_user_id не создаёт пользователя.
    user = get_user(db, event.max_user_id)
    report = (
        db.scalar(select(Report).where(Report.id == event.report_id, Report.user_id == user.id))
        if user is not None
        else None
    )
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")

    if event.confirmed:
        incident = db.get(Incident, report.incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        incident.description = f"{incident.description}\n---\n{report.message_text}"
        db.add(incident)
        db.commit()
        return BotReplyResponse(
            incident_id=report.incident_id,
            report_id=report.id,
            is_new_incident=False,
            reply_text="Спасибо, добавили ваше сообщение к найденному инциденту.",
        )

    with critical_section(db, "report-split", report.id):
        db.refresh(report)
        if report.user_id != user.id:  # заявку успели переназначить, пока ждали секцию
            raise HTTPException(status_code=404, detail="report not found")
        old_incident = db.scalar(select(Incident).where(Incident.id == report.incident_id))
        if old_incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        if old_incident.status == IncidentStatus.RESOLVED:
            # Закрытый инцидент — история; переносить из него заявки нельзя.
            db.commit()
            return BotReplyResponse(
                ok=False,
                incident_id=old_incident.id,
                reply_text=(
                    f"Инцидент №{old_incident.id} уже закрыт, изменить его нельзя. "
                    "Если проблема повторилась — опишите её новым сообщением."
                ),
            )

        other_reports = db.scalar(
            select(func.count()).select_from(Report).where(
                Report.incident_id == old_incident.id, Report.id != report.id
            )
        )
        if not other_reports:
            # Заявка уже одна в своём инциденте (повторное нажатие «это другое»
            # или повторная доставка webhook) — второй пустой инцидент не создаём.
            db.commit()
            return BotReplyResponse(
                incident_id=old_incident.id,
                is_new_incident=False,
                reply_text=f"Заявка уже вынесена в отдельный инцидент №{old_incident.id}.",
            )

        new_incident = Incident(
            building_id=old_incident.building_id,
            category=old_incident.category,
            description=report.message_text,
        )
        db.add(new_incident)
        db.flush()

        report.incident_id = new_incident.id
        db.add(report)
        db.commit()

    return BotReplyResponse(
        incident_id=new_incident.id,
        report_id=report.id,
        is_new_incident=True,
        reply_text=f"Понял, это отдельная проблема. Создали новый инцидент №{new_incident.id}.",
    )


@router.post("/status-query", response_model=BotReplyResponse)
def handle_status_query(event: StatusQueryEvent, db: Session = Depends(get_db)) -> BotReplyResponse:
    """Житель сам спрашивает "что с моей заявкой?" — не дожидаясь push при
    смене статуса. Читает данные, ничего не меняет."""
    # Только чтение: незнакомый max_user_id не создаёт пользователя.
    user = get_user(db, event.max_user_id)

    if user is None or not user.address_confirmed or user.building_id is None:
        return BotReplyResponse(
            needs_clarification=True,
            clarification_type="address",
            reply_text="Сначала подскажите адрес дома и подъезд — тогда смогу показать ваши заявки.",
        )

    incident_ids = db.scalars(select(Report.incident_id).where(Report.user_id == user.id).distinct()).all()
    if not incident_ids:
        return BotReplyResponse(
            reply_text="У вас пока нет поданных заявок. Опишите проблему одним сообщением, и я её зарегистрирую."
        )

    incidents = db.scalars(
        select(Incident).where(Incident.id.in_(incident_ids)).order_by(Incident.created_at.desc()).limit(5)
    ).all()

    lines = ["Ваши последние заявки:"]
    for incident in incidents:
        report_count = db.scalar(select(func.count()).select_from(Report).where(Report.incident_id == incident.id))
        extra = f" (всего пожаловалось: {report_count})" if report_count and report_count > 1 else ""
        lines.append(f"№{incident.id} «{category_label(incident.category)}» — {status_label(incident.status)}{extra}")

    return BotReplyResponse(reply_text="\n".join(lines))


def _notify_status_change_background(incident_id: int) -> None:
    """Уведомления шлём в фоне (после ответа боту), чтобы не задерживать
    webhook на N сетевых вызовах к MAX API. Открываем свою сессию — та,
    что была в запросе, к этому моменту уже закрыта."""
    db = SessionLocal()
    try:
        incident = db.scalar(select(Incident).where(Incident.id == incident_id))
        if incident is not None:
            notify_status_change(db, incident)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка фоновой рассылки уведомлений по инциденту %s", incident_id)
    finally:
        db.close()


def _notify_urgent_incident_background(incident_id: int) -> None:
    """Немедленное уведомление УК о только что созданном опасном инциденте —
    в фоне, тем же паттерном, что и уведомления о смене статуса."""
    db = SessionLocal()
    try:
        incident = db.scalar(select(Incident).where(Incident.id == incident_id))
        if incident is not None:
            notify_urgent_incident(db, incident)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка немедленного уведомления по срочному инциденту %s", incident_id)
    finally:
        db.close()


@router.post("/status-update", response_model=BotReplyResponse)
def handle_status_update(
    event: StatusUpdateEvent, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> BotReplyResponse:
    user = get_user(db, event.max_user_id)
    if user is None or user.role != UserRole.MANAGEMENT:
        raise HTTPException(status_code=403, detail="only management users can change incident status")
    # Сотрудник УК — владелец только инцидентов СВОЕГО дома: сотрудник УК
    # дома Б не может закрывать/переоткрывать заявки жителей дома А.
    # Без привязки к дому менять статусы нельзя вовсе.
    if user.building_id is None:
        raise HTTPException(status_code=403, detail="management user is not assigned to a building")
    return apply_status_update(event, background_tasks, db, user, restrict_to_building=user.building_id)


def apply_status_update(
    event: StatusUpdateEvent,
    background_tasks: BackgroundTasks,
    db: Session,
    user: User,
    *,
    restrict_to_building: int | None,
) -> BotReplyResponse:
    """Общая логика смены статуса. `restrict_to_building=None` — только для
    локального диспетчера веб-панели (доступна лишь с этого компьютера)."""

    # Сериализуем смены статуса одного инцидента: два одновременных «в работе»
    # (двойной клик, повтор webhook) иначе оба проходят проверку «статус уже
    # такой?» и пишут две записи в историю + дважды рассылают уведомления.
    # Ключ совпадает с дедупликацией (дом, категория): жалоба жителя не может
    # присоединиться к инциденту в момент его закрытия.
    incident = db.scalar(select(Incident).where(Incident.id == event.incident_id))
    if incident is None or (restrict_to_building is not None and incident.building_id != restrict_to_building):
        # Инцидент чужого дома неотличим от несуществующего — не раскрываем ID.
        raise HTTPException(status_code=404, detail="incident not found")

    with incident_dedup_lock(db, incident.building_id, incident.category):
        db.refresh(incident)
        if incident.status == event.new_status:
            db.commit()
            return BotReplyResponse(incident_id=incident.id, reply_text="Статус уже такой, изменений не внесено.")

        now = datetime.now(timezone.utc)
        incident.status = event.new_status
        incident.last_status_changed_at = now
        incident.sla_last_reminded_at = None
        incident.closed_at = now if event.new_status == IncidentStatus.RESOLVED else None

        db.add(incident)
        db.add(
            StatusUpdate(
                incident_id=incident.id,
                status=event.new_status,
                changed_by_user_id=user.id,
                source="management",
                note=event.note,
            )
        )
        db.commit()

    background_tasks.add_task(_notify_status_change_background, incident.id)

    return BotReplyResponse(incident_id=incident.id, reply_text=f"Статус инцидента №{incident.id} обновлён.")
