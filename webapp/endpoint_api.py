"""Приём заданий печати от endpoint-агентов — процессов на пользовательских
Windows ПК, учитывающих USB/WSD/прямые IP-принтеры (см.
docs/PRINTER_MONITORING_FORECASTING.md). ЭТО ЛОКАЛЬНЫЙ API ПЛОЩАДКИ, не
центра: endpoint-агент никогда не обращается к центральному серверу
напрямую — он шлёт данные на веб-сервер СВОЕЙ площадки (тот же процесс,
что показывает локальный дашборд), доступен в standalone- и agent-режимах.
Площадка сама пересылает эти задания в центр как обычные print_jobs через
уже существующий протокол агент->центр (см. collector/agent_sync.py,
PrintJob.endpoint_agent_id) — никакого отдельного протокола для этого не
требуется, endpoint-задания просто становятся ещё одним источником
локальных print_jobs, наравне с событиями Print Server.

Аутентификация — тот же принцип, что и у /api/v1/agent/* (bearer-токен,
хэш в БД, см. printaudit.security.agent_tokens), но токен принадлежит
EndpointAgent, не PrintServer, и НЕ работает для /api/v1/agent/*, и
наоборот."""
import logging
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from printaudit.ad_normalize import normalize_login
from printaudit.ad_settings import get_ad_settings
from printaudit.agent_settings import MODE_CENTRAL, get_agent_settings, is_agent_mode
from printaudit.config import get_settings
from printaudit.department_resolver import lookup_department_for_print_job_user
from printaudit.models import EndpointAgent, OutboxEvent, PrintJob
from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price
from printaudit.privacy import apply_document_name_policy
from printaudit.security.agent_tokens import hash_agent_token
from printaudit.timeutil import naive_utc, utcnow
from webapp.deps import get_db

logger = logging.getLogger("webapp.endpoint_api")

router = APIRouter(prefix="/api/v1/endpoint", tags=["endpoint"])

ENDPOINT_PROTOCOL_VERSION = 1
MAX_EVENTS_PER_BATCH = 500
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 МБ -- endpoint-пакеты меньше, чем у Print Server

MAX_USER_NAME_LENGTH = 200
MAX_PRINTER_NAME_LENGTH = 200
MAX_DOCUMENT_NAME_LENGTH = 500
MAX_HOSTNAME_LENGTH = 255
MAX_JOB_ID_LENGTH = 50
MAX_AGENT_VERSION_LENGTH = 50
MAX_LAST_ERROR_LENGTH = 2000
MAX_IDENTITY_LENGTH = 64

MIN_RECORD_ID = 0
MAX_RECORD_ID = 2_147_483_647
MAX_TOTAL_PAGES = 1_000_000
MIN_COPIES = 1
MAX_COPIES = 100_000
MAX_PAGES_PER_COPY = 1_000_000


def require_local_mode() -> None:
    """Endpoint-агенты шлют данные на СВОЙ сервер площадки — доступно и в
    standalone, и в agent режиме (там, где вообще есть локальная площадка).
    В central этих endpoint'ов как будто не существует: центр не принимает
    подключения от конечных ПК напрямую, только от Print Server/агентов
    площадок (см. webapp/agent_api.py::require_central_mode — тот же
    принцип, зеркально)."""
    if get_agent_settings().mode == MODE_CENTRAL:
        raise HTTPException(status_code=404, detail="Endpoint API недоступен в режиме APP_MODE=central")


def require_endpoint_agent(request: Request, db: Session = Depends(get_db)) -> EndpointAgent:
    agent_settings = get_agent_settings()
    if agent_settings.require_https and request.url.scheme != "https":
        raise HTTPException(status_code=400, detail="Endpoint API доступен только по HTTPS")

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Требуется заголовок Authorization: Bearer <токен>")
    raw_token = auth_header[len("Bearer "):].strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Пустой токен")

    token_hash = hash_agent_token(raw_token)
    agent = db.query(EndpointAgent).filter_by(token_hash=token_hash).first()
    if agent is None or agent.is_disabled or not token_hash:
        raise HTTPException(status_code=401, detail="Неверный, отозванный или отключённый токен endpoint-агента")
    return agent


def _check_protocol_version(payload_version: int) -> None:
    if payload_version != ENDPOINT_PROTOCOL_VERSION:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_protocol_version",
                "supported_protocol_version": ENDPOINT_PROTOCOL_VERSION,
                "received_protocol_version": payload_version,
            },
        )


def _check_identity(payload_endpoint_uuid: str, agent: EndpointAgent) -> None:
    if payload_endpoint_uuid != agent.uuid:
        raise HTTPException(status_code=400, detail="endpoint_uuid в теле запроса не совпадает с тем, кому принадлежит токен")


def _parse_time(raw: str) -> datetime:
    try:
        return naive_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError(f"Некорректное время: {raw!r}") from exc


class EndpointEventIn(BaseModel):
    record_id: int = Field(ge=MIN_RECORD_ID, le=MAX_RECORD_ID)
    job_id: Optional[str] = Field(default=None, max_length=MAX_JOB_ID_LENGTH)
    time_created: str
    user_name: str = Field(min_length=1, max_length=MAX_USER_NAME_LENGTH)
    document_name: Optional[str] = Field(default=None, max_length=MAX_DOCUMENT_NAME_LENGTH)
    printer_name: str = Field(min_length=1, max_length=MAX_PRINTER_NAME_LENGTH)
    total_pages: int = Field(ge=0, le=MAX_TOTAL_PAGES)
    copies: Optional[int] = Field(default=None, ge=MIN_COPIES, le=MAX_COPIES)
    pages_per_copy: Optional[int] = Field(default=None, ge=0, le=MAX_PAGES_PER_COPY)


class EndpointEventsBatchIn(BaseModel):
    protocol_version: int
    endpoint_uuid: str = Field(min_length=1, max_length=MAX_IDENTITY_LENGTH)
    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME_LENGTH)
    agent_version: str = Field(max_length=MAX_AGENT_VERSION_LENGTH)
    generated_at: str
    events: List[EndpointEventIn] = Field(default_factory=list, max_length=MAX_EVENTS_PER_BATCH)


class EndpointEventAck(BaseModel):
    record_id: int
    status: Literal["inserted", "duplicate", "rejected"]
    error: Optional[str] = None


class EndpointEventsBatchOut(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: List[EndpointEventAck]


class EndpointHeartbeatIn(BaseModel):
    protocol_version: int
    endpoint_uuid: str = Field(min_length=1, max_length=MAX_IDENTITY_LENGTH)
    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME_LENGTH)
    agent_version: str = Field(max_length=MAX_AGENT_VERSION_LENGTH)
    pending_queue_size: int = Field(default=0, ge=0, le=100_000_000)
    failed_queue_size: int = Field(default=0, ge=0, le=100_000_000)
    last_error: Optional[str] = Field(default=None, max_length=MAX_LAST_ERROR_LENGTH)


class EndpointHeartbeatOut(BaseModel):
    ok: bool


@router.post(
    "/events/batch",
    response_model=EndpointEventsBatchOut,
    dependencies=[Depends(require_local_mode)],
)
def endpoint_events_batch(
    payload: EndpointEventsBatchIn,
    db: Session = Depends(get_db),
    agent: EndpointAgent = Depends(require_endpoint_agent),
):
    """Каждое событие становится обычным PrintJob (source_computer =
    hostname агента, endpoint_agent_id = сам агент, print_server_id
    остаётся NULL — это НЕ задание Print Server) — те же тариф/отдел/
    privacy-политика, что и у любого другого задания печати, ничем не
    отличается для отчётов/журнала. В agent-режиме сразу ставится в тот же
    outbox, что и задания Print Server (см. collect_print_events.py) —
    единая доставка в центр, без отдельного протокола."""
    _check_protocol_version(payload.protocol_version)
    _check_identity(payload.endpoint_uuid, agent)

    settings = get_settings()
    ad_settings = get_ad_settings()
    outbox_enabled = is_agent_mode()

    results: List[EndpointEventAck] = []
    accepted = duplicates = rejected = 0

    for event in payload.events:
        try:
            with db.begin_nested():
                existing = (
                    db.query(PrintJob)
                    .filter_by(endpoint_agent_id=agent.id, record_id=event.record_id)
                    .first()
                )
                if existing is not None:
                    duplicates += 1
                    results.append(EndpointEventAck(record_id=event.record_id, status="duplicate"))
                    continue

                if not event.printer_name.strip() or not event.user_name.strip():
                    raise ValueError("printer_name и user_name обязательны")

                time_created = _parse_time(event.time_created)
                printer_queue = get_or_create_printer_queue(db, event.printer_name, endpoint_agent_id=agent.id)
                resolution = resolve_price(db, printer_queue, time_created, settings)
                cost = round(event.total_pages * resolution.price_per_page, 2)
                department_id = lookup_department_for_print_job_user(
                    db, event.user_name, ad_domain=ad_settings.domain or None
                )

                job = PrintJob(
                    site_code=settings.site_code, site_id=agent.site_id, endpoint_agent_id=agent.id,
                    record_id=event.record_id, job_id=event.job_id, time_created=time_created,
                    user_name=event.user_name, user_login_normalized=normalize_login(event.user_name),
                    document_name=apply_document_name_policy(event.document_name, settings.document_name_policy),
                    printer_name=event.printer_name, source_computer=payload.hostname,
                    total_pages=event.total_pages, copies=event.copies, pages_per_copy=event.pages_per_copy,
                    is_color=resolution.is_color, color_source=resolution.color_source,
                    department_id=department_id, printer_queue_id=printer_queue.id,
                    price_rule_id=resolution.price_rule_id, price_per_page=resolution.price_per_page,
                    currency=resolution.currency, cost=cost,
                )
                db.add(job)
                if outbox_enabled:
                    db.add(OutboxEvent(print_job=job))
                db.flush()
            accepted += 1
            results.append(EndpointEventAck(record_id=event.record_id, status="inserted"))
        except Exception as exc:  # noqa: BLE001 - одно плохое событие не должно валить весь пакет
            rejected += 1
            logger.warning(
                "Отклонено событие record_id=%s от endpoint-агента=%s: %s", event.record_id, agent.uuid, exc,
            )
            results.append(EndpointEventAck(record_id=event.record_id, status="rejected", error=str(exc)[:300]))

    now = utcnow()
    agent.last_contact_at = now
    if rejected == 0:
        agent.last_sync_at = now
    db.commit()
    return EndpointEventsBatchOut(accepted=accepted, duplicates=duplicates, rejected=rejected, results=results)


@router.post(
    "/heartbeat",
    response_model=EndpointHeartbeatOut,
    dependencies=[Depends(require_local_mode)],
)
def endpoint_heartbeat(
    payload: EndpointHeartbeatIn,
    db: Session = Depends(get_db),
    agent: EndpointAgent = Depends(require_endpoint_agent),
):
    _check_protocol_version(payload.protocol_version)
    _check_identity(payload.endpoint_uuid, agent)

    now = utcnow()
    agent.last_heartbeat_at = now
    agent.last_contact_at = now
    agent.agent_version = payload.agent_version
    agent.pending_queue_size = payload.pending_queue_size
    agent.failed_queue_size = payload.failed_queue_size
    agent.last_error = payload.last_error
    db.commit()
    return EndpointHeartbeatOut(ok=True)


class MaxEndpointBodySizeMiddleware:
    """Тот же принцип, что и webapp.agent_api.MaxBodySizeMiddleware, для
    префикса /api/v1/endpoint — endpoint-пакеты меньше (один ПК, не
    Print Server), поэтому свой, более скромный лимит."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES, path_prefix: str = "/api/v1/endpoint"):
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefix = path_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return

        content_length = None
        for key, value in scope.get("headers") or []:
            if key == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    content_length = None
                break

        if content_length is not None and content_length > self.max_bytes:
            await self._reject(send)
            return

        total = 0

        async def guarded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Тело запроса превышает допустимый размер")
            return message

        await self.app(scope, guarded_receive, send)

    @staticmethod
    async def _reject(send):
        await send(
            {"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]}
        )
        await send({"type": "http.response.body", "body": b'{"detail":"Request body too large"}'})
