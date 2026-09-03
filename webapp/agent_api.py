"""Центральный API для агентов (Print Server на площадках) — версия v1.

Аутентификация: bearer-токен, привязанный к ОДНОМУ PrintServer (см.
printaudit.security.agent_tokens). Сессионные cookie/CSRF здесь не
участвуют вообще — это не браузерный роут, а API для доверенной машины.

Направление передачи данных: агент САМ инициирует исходящее HTTPS-
соединение к центру (POST). Центр никогда не подключается к площадке —
никаких входящих портов на Print Server открывать не нужно (см.
docs/MULTISITE_ARCHITECTURE.md).

Идемпотентность на приём: (print_server_id, record_id) — тот же ключ, что
и в printaudit.models.PrintJob. Повторная отправка уже принятого события
(или частично повторяющийся пакет) не создаёт дублей: строка просто не
вставляется повторно и помечается в ответе как "duplicate", не ошибка."""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from printaudit.agent_settings import PROTOCOL_VERSION, get_agent_settings
from printaudit.models import Department, PrintJob, PrintServer
from printaudit.printers.resolver import get_or_create_printer_queue
from printaudit.security.agent_tokens import hash_agent_token
from printaudit.sites import compute_status
from printaudit.timeutil import naive_utc, utcnow
from webapp.deps import get_db

logger = logging.getLogger("webapp.agent_api")

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

VALID_COLOR_SOURCES = ("event", "queue", "unknown")


class AgentEventIn(BaseModel):
    record_id: int
    job_id: Optional[str] = None
    time_created: str
    user_name: str
    user_login_normalized: Optional[str] = None
    document_name: Optional[str] = None
    printer_name: str
    source_computer: Optional[str] = None
    total_pages: int
    copies: Optional[int] = None
    pages_per_copy: Optional[int] = None
    is_color: Optional[bool] = None
    color_source: str = "unknown"
    department_name: Optional[str] = None
    price_per_page: Optional[float] = None
    currency: Optional[str] = None
    cost: Optional[float] = None
    extra: Optional[dict] = None


class AgentEventsBatchIn(BaseModel):
    protocol_version: int
    site_uuid: str
    print_server_uuid: str
    generated_at: str
    events: List[AgentEventIn] = Field(default_factory=list)


class AgentEventAck(BaseModel):
    record_id: int
    status: str  # inserted | duplicate | rejected
    error: Optional[str] = None


class AgentEventsBatchOut(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: List[AgentEventAck]


class AgentHeartbeatIn(BaseModel):
    protocol_version: int
    site_uuid: str
    print_server_uuid: str
    agent_version: str
    pending_queue_size: int = 0
    last_error: Optional[str] = None


class AgentHeartbeatOut(BaseModel):
    ok: bool
    server_status: str
    protocol_version: int = PROTOCOL_VERSION


def require_agent_print_server(request: Request, db: Session = Depends(get_db)) -> PrintServer:
    """Аутентификация агента по bearer-токену. НИКОГДА не логирует сырой
    токен (см. printaudit.security.agent_tokens) и не возвращает разные
    сообщения об ошибке для "нет такого токена" vs "токен отозван/сервер
    отключён" — оба случая должны выглядеть одинаково для того, кто пытается
    подобрать токен."""
    agent_settings = get_agent_settings()
    if agent_settings.require_https and request.url.scheme != "https":
        raise HTTPException(status_code=400, detail="Агентский API доступен только по HTTPS")

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Требуется заголовок Authorization: Bearer <токен>")
    raw_token = auth_header[len("Bearer "):].strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Пустой токен агента")

    token_hash = hash_agent_token(raw_token)
    server = db.query(PrintServer).filter_by(token_hash=token_hash).first()
    if server is None or server.is_disabled or not token_hash:
        raise HTTPException(status_code=401, detail="Неверный, отозванный или отключённый токен агента")
    return server


def _check_identity(payload_site_uuid: str, payload_server_uuid: str, server: PrintServer) -> None:
    """Токен уже однозначно определяет PrintServer — этот доп. запрос
    сверяет, что заявленные в теле site_uuid/print_server_uuid совпадают с
    тем, кому реально принадлежит токен (защита от неверно
    сконфигурированного агента, отправляющего события "от чужого имени")."""
    if payload_server_uuid != server.uuid or payload_site_uuid != server.site.uuid:
        raise HTTPException(
            status_code=400,
            detail="site_uuid/print_server_uuid в теле запроса не совпадают с тем, кому принадлежит токен",
        )


def _get_or_create_department_id(db: Session, name: Optional[str]) -> Optional[int]:
    if not name or not name.strip():
        return None
    name = name.strip()
    dept = db.query(Department).filter_by(name=name).first()
    if dept is None:
        dept = Department(name=name, is_active=True)
        db.add(dept)
        db.flush()
    return dept.id


def _parse_time_created(raw: str):
    from datetime import datetime

    try:
        return naive_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError(f"Некорректный time_created: {raw!r}") from exc


@router.post("/events/batch", response_model=AgentEventsBatchOut)
def agent_events_batch(
    payload: AgentEventsBatchIn,
    db: Session = Depends(get_db),
    server: PrintServer = Depends(require_agent_print_server),
):
    _check_identity(payload.site_uuid, payload.print_server_uuid, server)

    results: List[AgentEventAck] = []
    accepted = duplicates = rejected = 0

    for event in payload.events:
        try:
            with db.begin_nested():
                existing = (
                    db.query(PrintJob)
                    .filter_by(print_server_id=server.id, record_id=event.record_id)
                    .first()
                )
                if existing is not None:
                    duplicates += 1
                    results.append(AgentEventAck(record_id=event.record_id, status="duplicate"))
                    continue

                if event.total_pages < 0:
                    raise ValueError("total_pages не может быть отрицательным")
                if event.color_source not in VALID_COLOR_SOURCES:
                    raise ValueError(f"color_source должен быть одним из {VALID_COLOR_SOURCES}")
                if not event.printer_name.strip() or not event.user_name.strip():
                    raise ValueError("printer_name и user_name обязательны")

                time_created = _parse_time_created(event.time_created)
                department_id = _get_or_create_department_id(db, event.department_name)
                printer_queue = get_or_create_printer_queue(db, event.printer_name, server.id)

                job = PrintJob(
                    site_code=server.site.site_code,
                    site_id=server.site_id,
                    print_server_id=server.id,
                    record_id=event.record_id,
                    job_id=event.job_id,
                    time_created=time_created,
                    user_name=event.user_name,
                    user_login_normalized=event.user_login_normalized,
                    document_name=event.document_name,
                    printer_name=event.printer_name,
                    source_computer=event.source_computer,
                    total_pages=event.total_pages,
                    copies=event.copies,
                    pages_per_copy=event.pages_per_copy,
                    is_color=event.is_color,
                    color_source=event.color_source,
                    department_id=department_id,
                    printer_queue_id=printer_queue.id,
                    price_per_page=event.price_per_page,
                    currency=event.currency,
                    cost=event.cost,
                )
                db.add(job)
                db.flush()
            accepted += 1
            results.append(AgentEventAck(record_id=event.record_id, status="inserted"))
        except Exception as exc:  # noqa: BLE001 - одно плохое событие не должно валить весь пакет
            rejected += 1
            logger.warning(
                "Отклонено событие record_id=%s от print_server=%s: %s",
                event.record_id, server.uuid, exc,
            )
            results.append(AgentEventAck(record_id=event.record_id, status="rejected", error=str(exc)[:300]))

    server.last_sync_at = utcnow()
    server.last_error = None
    db.commit()
    return AgentEventsBatchOut(accepted=accepted, duplicates=duplicates, rejected=rejected, results=results)


@router.post("/heartbeat", response_model=AgentHeartbeatOut)
def agent_heartbeat(
    payload: AgentHeartbeatIn,
    db: Session = Depends(get_db),
    server: PrintServer = Depends(require_agent_print_server),
):
    _check_identity(payload.site_uuid, payload.print_server_uuid, server)

    server.last_heartbeat_at = utcnow()
    server.agent_version = payload.agent_version
    server.protocol_version = payload.protocol_version
    server.pending_queue_size = payload.pending_queue_size
    server.last_error = payload.last_error
    db.commit()
    return AgentHeartbeatOut(ok=True, server_status=compute_status(server))
