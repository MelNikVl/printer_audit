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
вставляется повторно и помечается в ответе как "duplicate", не ошибка.

**Доступность по режиму.** Этот роутер подключается в webapp/main.py
БЕЗУСЛОВНО (см. комментарий там же), но каждый endpoint требует
APP_MODE=central (см. require_central_mode ниже) — в standalone/agent
режимах оба endpoint'а отвечают 404, как будто их вообще нет, ДО проверки
токена (агент/standalone-сервер не должен даже узнать, валиден ли
предъявленный токен).

**Лимиты на входящий пакет** (см. также docs/MULTISITE_ARCHITECTURE.md):
  - MAX_EVENTS_PER_BATCH событий за один пакет;
  - MAX_BODY_BYTES байт на тело запроса (проверяется ДО разбора JSON —
    см. MaxBodySizeMiddleware, подключается в webapp/main.py только для
    префикса /api/v1/agent);
  - длины строковых полей ограничены тем же размером, что и
    соответствующая колонка БД (см. printaudit.models.PrintJob/Department) —
    без этого превышающая лимит строка тихо обрежется на SQLite, но упадёт
    с ошибкой на PostgreSQL;
  - record_id ограничен диапазоном 32-битного знакового INTEGER (то, во что
    реально мапится SQLAlchemy Integer на PostgreSQL — превышение привело
    бы к ошибке на этом бэкенде);
  - total_pages/copies/pages_per_copy/price_per_page/cost — разумные
    верхние границы и запрет NaN/Infinity/отрицательных денежных значений
    (JSON, распарсенный стандартным json.loads, ДОПУСКАЕТ литералы NaN/
    Infinity/-Infinity как расширение Python — если полагаться только на
    `ge=0`, отрицательные и NaN отсекаются автоматически (сравнение с NaN
    всегда False), но для ясного сообщения об ошибке NaN/Infinity
    проверяются явно, см. _reject_non_finite).
"""
import logging
import math
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from printaudit.agent_settings import MODE_CENTRAL, PROTOCOL_VERSION, get_agent_settings
from printaudit.models import Department, PrintJob, PrintServer
from printaudit.printers.resolver import get_or_create_printer_queue
from printaudit.security.agent_tokens import hash_agent_token
from printaudit.sites import compute_status
from printaudit.timeutil import naive_utc, utcnow
from webapp.deps import get_db

logger = logging.getLogger("webapp.agent_api")

# --- Лимиты пакета (см. docstring модуля) -----------------------------------
MAX_EVENTS_PER_BATCH = 1000
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 МБ — с запасом на MAX_EVENTS_PER_BATCH событий

# Длины строк — ровно те же, что и у соответствующих колонок БД (см.
# printaudit/models.py), чтобы никогда не упасть на PostgreSQL с ошибкой
# "value too long for type character varying(N)" уже ПОСЛЕ прохождения
# бизнес-валидации.
MAX_USER_NAME_LENGTH = 200
MAX_LOGIN_LENGTH = 200
MAX_DOCUMENT_NAME_LENGTH = 500
MAX_PRINTER_NAME_LENGTH = 200
MAX_SOURCE_COMPUTER_LENGTH = 200
MAX_DEPARTMENT_NAME_LENGTH = 200
MAX_JOB_ID_LENGTH = 50
MAX_COLOR_SOURCE_LENGTH = 10
MAX_CURRENCY_LENGTH = 10
MAX_IDENTITY_LENGTH = 64  # site_uuid/print_server_uuid — UUID (36), с запасом
MAX_LAST_ERROR_LENGTH = 2000  # как SyncRun.error_message
MAX_AGENT_VERSION_LENGTH = 50

# record_id хранится в PrintJob.record_id — SQLAlchemy Integer, что на
# PostgreSQL — 32-битный знаковый int (в отличие от SQLite, где предела
# практически нет) — выход за диапазон должен быть отклонён валидацией
# API, а не превращаться в ошибку INSERT на проде.
MIN_RECORD_ID = 0
MAX_RECORD_ID = 2_147_483_647

MAX_TOTAL_PAGES = 1_000_000
MIN_COPIES = 1
MAX_COPIES = 100_000
MAX_PAGES_PER_COPY = 1_000_000
MAX_PRICE_PER_PAGE = 1_000_000.0
MAX_COST = 100_000_000.0


def _reject_non_finite(value: Optional[float], field_name: str) -> Optional[float]:
    """NaN/Infinity/-Infinity технически парсятся стандартным json.loads (это
    расширение Python, не строгий JSON) — ge/le сами по себе отсекли бы их
    (сравнение с NaN всегда False), но с невнятным сообщением "not a valid
    number" вместо явного объяснения, что именно не так."""
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"{field_name}: значение должно быть конечным числом (NaN/Infinity запрещены)")
    if value < 0:
        raise ValueError(f"{field_name}: денежное значение не может быть отрицательным")
    return value


class AgentEventIn(BaseModel):
    record_id: int = Field(ge=MIN_RECORD_ID, le=MAX_RECORD_ID)
    job_id: Optional[str] = Field(default=None, max_length=MAX_JOB_ID_LENGTH)
    time_created: str
    user_name: str = Field(min_length=1, max_length=MAX_USER_NAME_LENGTH)
    user_login_normalized: Optional[str] = Field(default=None, max_length=MAX_LOGIN_LENGTH)
    document_name: Optional[str] = Field(default=None, max_length=MAX_DOCUMENT_NAME_LENGTH)
    printer_name: str = Field(min_length=1, max_length=MAX_PRINTER_NAME_LENGTH)
    source_computer: Optional[str] = Field(default=None, max_length=MAX_SOURCE_COMPUTER_LENGTH)
    total_pages: int = Field(ge=0, le=MAX_TOTAL_PAGES)
    copies: Optional[int] = Field(default=None, ge=MIN_COPIES, le=MAX_COPIES)
    pages_per_copy: Optional[int] = Field(default=None, ge=0, le=MAX_PAGES_PER_COPY)
    is_color: Optional[bool] = None
    # Literal, не просто str+max_length -- невалидное значение (например,
    # опечатка) отклоняет ВЕСЬ пакет на уровне схемы (422, до касания БД),
    # а не тихо долетает до бизнес-проверки внутри цикла по событиям.
    color_source: Literal["event", "queue", "unknown"] = "unknown"
    department_name: Optional[str] = Field(default=None, max_length=MAX_DEPARTMENT_NAME_LENGTH)
    price_per_page: Optional[float] = Field(default=None, le=MAX_PRICE_PER_PAGE)
    currency: Optional[str] = Field(default=None, max_length=MAX_CURRENCY_LENGTH)
    cost: Optional[float] = Field(default=None, le=MAX_COST)
    extra: Optional[dict] = None

    @field_validator("price_per_page")
    @classmethod
    def _validate_price_per_page(cls, v):
        return _reject_non_finite(v, "price_per_page")

    @field_validator("cost")
    @classmethod
    def _validate_cost(cls, v):
        return _reject_non_finite(v, "cost")


class AgentEventsBatchIn(BaseModel):
    protocol_version: int
    site_uuid: str = Field(min_length=1, max_length=MAX_IDENTITY_LENGTH)
    print_server_uuid: str = Field(min_length=1, max_length=MAX_IDENTITY_LENGTH)
    generated_at: str
    events: List[AgentEventIn] = Field(default_factory=list, max_length=MAX_EVENTS_PER_BATCH)


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
    site_uuid: str = Field(min_length=1, max_length=MAX_IDENTITY_LENGTH)
    print_server_uuid: str = Field(min_length=1, max_length=MAX_IDENTITY_LENGTH)
    agent_version: str = Field(max_length=MAX_AGENT_VERSION_LENGTH)
    pending_queue_size: int = Field(default=0, ge=0, le=100_000_000)
    failed_queue_size: int = Field(default=0, ge=0, le=100_000_000)
    last_error: Optional[str] = Field(default=None, max_length=MAX_LAST_ERROR_LENGTH)


class AgentHeartbeatOut(BaseModel):
    ok: bool
    server_status: str
    protocol_version: int = PROTOCOL_VERSION


router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


def require_central_mode() -> None:
    """В standalone/agent режимах этих endpoint'ов как будто не существует —
    404, а не 403: приложение в этих режимах НЕ является central-сервером,
    и с точки зрения агента/клиента это просто "такого адреса нет", а не
    "есть, но доступ запрещён". Проверяется ДО аутентификации по токену —
    неверный APP_MODE не должен давать возможность даже проверить, валиден
    ли токен."""
    if get_agent_settings().mode != MODE_CENTRAL:
        raise HTTPException(
            status_code=404,
            detail="Агентский API недоступен: сервер не в режиме APP_MODE=central",
        )


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


def _check_protocol_version(payload_version: int) -> None:
    """Машинно-читаемая ошибка (структурированный detail, не просто текст) —
    агент может программно отличить "неподдерживаемая версия протокола" от
    любой другой 4xx-ошибки и, например, залогировать это отдельно как
    "требуется обновление агента", вместо бесконечных retry с backoff (это
    НЕ временный сбой, retry его не исправит)."""
    if payload_version != PROTOCOL_VERSION:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_protocol_version",
                "supported_protocol_version": PROTOCOL_VERSION,
                "received_protocol_version": payload_version,
            },
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


@router.post(
    "/events/batch",
    response_model=AgentEventsBatchOut,
    dependencies=[Depends(require_central_mode)],
)
def agent_events_batch(
    payload: AgentEventsBatchIn,
    db: Session = Depends(get_db),
    server: PrintServer = Depends(require_agent_print_server),
):
    _check_protocol_version(payload.protocol_version)
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

                # printer_name/user_name уже min_length=1 на уровне схемы —
                # но это не ловит строки из одних пробелов ("   "), поэтому
                # добивается здесь как бизнес-проверка (не структурная).
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

    now = utcnow()
    server.last_contact_at = now
    if rejected == 0:
        # last_sync_at означает "успешно синхронизировано", не просто
        # "агент достучался" — пакет, где ХОТЬ ЧТО-ТО отклонено, не в счёт
        # (см. требование "если весь пакет отклонён, не отмечай его как
        # успешную синхронизацию"; распространяется и на частичный reject —
        # партия не полностью синхронизирована, пока отклонённые события не
        # будут исправлены и присланы заново).
        server.last_sync_at = now
        server.last_ingest_error = None
    else:
        first_error = next((r.error for r in results if r.status == "rejected" and r.error), None)
        server.last_ingest_error = (
            f"Отклонено {rejected} из {len(payload.events)} событий"
            + (f": {first_error}" if first_error else "")
        )[:MAX_LAST_ERROR_LENGTH]
    db.commit()
    return AgentEventsBatchOut(accepted=accepted, duplicates=duplicates, rejected=rejected, results=results)


@router.post(
    "/heartbeat",
    response_model=AgentHeartbeatOut,
    dependencies=[Depends(require_central_mode)],
)
def agent_heartbeat(
    payload: AgentHeartbeatIn,
    db: Session = Depends(get_db),
    server: PrintServer = Depends(require_agent_print_server),
):
    _check_protocol_version(payload.protocol_version)
    _check_identity(payload.site_uuid, payload.print_server_uuid, server)

    now = utcnow()
    server.last_heartbeat_at = now
    server.last_contact_at = now
    server.agent_version = payload.agent_version
    server.protocol_version = payload.protocol_version
    server.pending_queue_size = payload.pending_queue_size
    server.failed_queue_size = payload.failed_queue_size
    server.last_error = payload.last_error
    db.commit()
    return AgentHeartbeatOut(ok=True, server_status=compute_status(server))


class MaxBodySizeMiddleware:
    """Чистый ASGI-middleware (не BaseHTTPMiddleware, который сам буферизует
    тело целиком в память до нашей проверки) — обрывает приём тела запроса
    для префикса /api/v1/agent, как только превышен MAX_BODY_BYTES, ДО того
    как FastAPI/pydantic начнут его разбирать.

    Основная защита — быстрая проверка заголовка Content-Length (все
    настоящие агенты шлют его: httpx всегда вычисляет длину JSON-тела
    заранее, не использует chunked transfer). Дополнительно, на случай
    отсутствующего/лживого Content-Length, суммарный размер тела считается
    и во время самого чтения потока — превышение прерывает запрос
    исключением, которое подхватывает штатный обработчик HTTPException в
    webapp/main.py и возвращает 413."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES, path_prefix: str = "/api/v1/agent"):
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
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"detail":"Request body too large"}'})
