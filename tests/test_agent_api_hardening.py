"""Pre-merge hardening тесты для webapp/agent_api.py:
  - агентский API доступен ТОЛЬКО в APP_MODE=central;
  - protocol_version проверяется на обоих endpoint'ах, структурированная
    ошибка, без изменения БД;
  - лимиты пакета: число событий, размер тела, длины строк, диапазоны чисел,
    запрет NaN/Infinity/отрицательных денежных значений;
  - last_sync_at/last_contact_at/last_ingest_error — правильная семантика;
  - HTTPS за доверенным реверс-прокси (X-Forwarded-Proto), недоверенный
    прокси игнорируется;
  - при отклонении структурно невалидного события в БД не остаётся ничего
    (весь пакет отклоняется на уровне схемы, ни одна строка не пишется).
"""
import math
from dataclasses import dataclass

from tests.conftest import login_as


@dataclass
class _Registered:
    site_uuid: str
    site_code: str
    server_uuid: str
    server_id: int
    token: str


def _register_print_server(session, site_code="ALMATY", server_name="PRN01"):
    from printaudit.security.agent_tokens import generate_agent_token, hash_agent_token
    from printaudit.sites import get_or_create_print_server, get_or_create_site
    from printaudit.timeutil import utcnow

    site = get_or_create_site(session, site_code, name=site_code)
    server = get_or_create_print_server(session, site, server_name)
    raw_token = generate_agent_token()
    server.token_hash = hash_agent_token(raw_token)
    server.token_created_at = utcnow()
    session.commit()
    return _Registered(
        site_uuid=site.uuid, site_code=site.site_code,
        server_uuid=server.uuid, server_id=server.id, token=raw_token,
    )


def _batch_payload(reg: _Registered, events, protocol_version=1):
    return {
        "protocol_version": protocol_version,
        "site_uuid": reg.site_uuid,
        "print_server_uuid": reg.server_uuid,
        "generated_at": "2026-09-03T10:00:00+00:00",
        "events": events,
    }


def _heartbeat_payload(reg: _Registered, protocol_version=1, **overrides):
    payload = {
        "protocol_version": protocol_version,
        "site_uuid": reg.site_uuid,
        "print_server_uuid": reg.server_uuid,
        "agent_version": "1.0",
        "pending_queue_size": 0,
        "failed_queue_size": 0,
    }
    payload.update(overrides)
    return payload


def _event(record_id, **overrides):
    base = {
        "record_id": record_id,
        "job_id": str(record_id),
        "time_created": "2026-09-03T09:00:00+00:00",
        "user_name": "DOMAIN\\ivanov",
        "printer_name": "HP-3F-BW",
        "total_pages": 5,
        "is_color": None,
        "color_source": "unknown",
    }
    base.update(overrides)
    return base


def _setup(monkeypatch, session, mode="central"):
    monkeypatch.setenv("APP_MODE", mode)
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    return _register_print_server(session)


# ---------------------------------------------------------------------------
# 1. Доступность только в APP_MODE=central
# ---------------------------------------------------------------------------


def test_events_batch_returns_404_in_standalone_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session, mode="standalone")
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 404


def test_events_batch_returns_404_in_agent_mode_even_with_valid_token(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session, mode="agent")
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 404

    from printaudit.database import SessionLocal as SL
    from printaudit.models import PrintJob

    s = SL()
    try:
        assert s.query(PrintJob).count() == 0
    finally:
        s.close()


def test_heartbeat_returns_404_outside_central_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session, mode="agent")
    session.close()

    resp = http_client.post(
        "/api/v1/agent/heartbeat",
        json=_heartbeat_payload(reg),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 404


def test_events_batch_404_even_without_any_authorization_header_outside_central(http_client, monkeypatch):
    """Режим проверяется ДО токена -- не central означает 404 независимо от
    того, есть ли вообще заголовок Authorization."""
    monkeypatch.setenv("APP_MODE", "standalone")
    resp = http_client.post("/api/v1/agent/events/batch", json={
        "protocol_version": 1, "site_uuid": "x", "print_server_uuid": "y",
        "generated_at": "2026-09-03T10:00:00+00:00", "events": [],
    })
    assert resp.status_code == 404


def test_events_batch_works_normally_in_central_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session, mode="central")
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


# ---------------------------------------------------------------------------
# 2. protocol_version
# ---------------------------------------------------------------------------


def test_events_batch_rejects_unsupported_protocol_version_with_machine_readable_error(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)], protocol_version=99),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "unsupported_protocol_version"
    assert detail["supported_protocol_version"] == 1
    assert detail["received_protocol_version"] == 99


def test_events_batch_unsupported_protocol_version_writes_nothing_to_db(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1), _event(2)], protocol_version=2),
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        assert session.query(PrintJob).count() == 0
    finally:
        session.close()


def test_heartbeat_rejects_unsupported_protocol_version(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/heartbeat",
        json=_heartbeat_payload(reg, protocol_version=7),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_protocol_version"


# ---------------------------------------------------------------------------
# 3. Лимиты пакета
# ---------------------------------------------------------------------------


def test_events_batch_rejects_too_many_events(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_EVENTS_PER_BATCH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    events = [_event(i) for i in range(MAX_EVENTS_PER_BATCH + 1)]
    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, events),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_events_batch_accepts_exactly_max_events(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_EVENTS_PER_BATCH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    events = [_event(i) for i in range(MAX_EVENTS_PER_BATCH)]
    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, events),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == MAX_EVENTS_PER_BATCH


def test_events_batch_rejects_oversized_body_via_content_length(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_BODY_BYTES

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    huge_doc = "x" * (MAX_BODY_BYTES + 1000)
    resp = http_client.post(
        "/api/v1/agent/events/batch",
        content=(
            '{"protocol_version":1,"site_uuid":"' + reg.site_uuid + '","print_server_uuid":"'
            + reg.server_uuid + '","generated_at":"2026-09-03T10:00:00+00:00","events":[],'
            + '"padding":"' + huge_doc + '"}'
        ),
        headers={"Authorization": f"Bearer {reg.token}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_events_batch_within_body_limit_is_processed_normally(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 200


def test_events_batch_rejects_oversized_user_name(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_USER_NAME_LENGTH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, user_name="a" * (MAX_USER_NAME_LENGTH + 1))]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_events_batch_rejects_oversized_printer_name(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_PRINTER_NAME_LENGTH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, printer_name="p" * (MAX_PRINTER_NAME_LENGTH + 1))]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_events_batch_rejects_oversized_document_name(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_DOCUMENT_NAME_LENGTH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, document_name="d" * (MAX_DOCUMENT_NAME_LENGTH + 1))]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_events_batch_rejects_oversized_source_computer(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_SOURCE_COMPUTER_LENGTH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, source_computer="c" * (MAX_SOURCE_COMPUTER_LENGTH + 1))]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_events_batch_rejects_oversized_department_name(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_DEPARTMENT_NAME_LENGTH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, department_name="d" * (MAX_DEPARTMENT_NAME_LENGTH + 1))]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_heartbeat_rejects_oversized_last_error(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_LAST_ERROR_LENGTH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/heartbeat",
        json=_heartbeat_payload(reg, last_error="e" * (MAX_LAST_ERROR_LENGTH + 1)),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_events_batch_rejects_record_id_out_of_range(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_RECORD_ID

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    for bad_record_id in (-1, MAX_RECORD_ID + 1):
        resp = http_client.post(
            "/api/v1/agent/events/batch",
            json=_batch_payload(reg, [_event(bad_record_id)]),
            headers={"Authorization": f"Bearer {reg.token}"},
        )
        assert resp.status_code == 422, bad_record_id


def test_events_batch_rejects_out_of_range_total_pages_copies_pages_per_copy(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_COPIES, MAX_PAGES_PER_COPY, MAX_TOTAL_PAGES

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    bad_events = [
        _event(1, total_pages=-1),
        _event(2, total_pages=MAX_TOTAL_PAGES + 1),
        _event(3, copies=0),
        _event(4, copies=MAX_COPIES + 1),
        _event(5, pages_per_copy=-1),
        _event(6, pages_per_copy=MAX_PAGES_PER_COPY + 1),
    ]
    for evt in bad_events:
        resp = http_client.post(
            "/api/v1/agent/events/batch",
            json=_batch_payload(reg, [evt]),
            headers={"Authorization": f"Bearer {reg.token}"},
        )
        assert resp.status_code == 422, evt


def test_events_batch_rejects_negative_price_and_cost(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    for evt in (_event(1, price_per_page=-0.01), _event(2, cost=-1.0)):
        resp = http_client.post(
            "/api/v1/agent/events/batch",
            json=_batch_payload(reg, [evt]),
            headers={"Authorization": f"Bearer {reg.token}"},
        )
        assert resp.status_code == 422, evt


def test_events_batch_rejects_nan_and_infinity_money_values(http_client, monkeypatch):
    """Стандартный json.loads (в отличие от строгого JSON) допускает
    литералы NaN/Infinity/-Infinity — если не проверять явно, они пройдут
    JSON-разбор и должны быть отклонены уже валидацией схемы."""
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    import json

    for bad_value_literal in ("NaN", "Infinity", "-Infinity"):
        raw_body = (
            '{"protocol_version":1,"site_uuid":"' + reg.site_uuid + '","print_server_uuid":"'
            + reg.server_uuid + '","generated_at":"2026-09-03T10:00:00+00:00","events":['
            + '{"record_id":1,"time_created":"2026-09-03T09:00:00+00:00","user_name":"u",'
            + '"printer_name":"p","total_pages":1,"cost":' + bad_value_literal + "}]}"
        )
        # Подтверждаем, что это вообще валидный ввод для json.loads (иначе тест
        # ничего не проверяет) -- сам API получает это как сырые байты тела.
        json.loads(raw_body)
        resp = http_client.post(
            "/api/v1/agent/events/batch",
            content=raw_body,
            headers={"Authorization": f"Bearer {reg.token}", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422, bad_value_literal

    from printaudit.models import PrintJob

    s = SessionLocal()
    try:
        assert s.query(PrintJob).count() == 0
    finally:
        s.close()


def test_events_batch_with_structurally_invalid_event_rejects_whole_batch_and_writes_nothing(http_client, monkeypatch):
    """Структурно некорректное событие (провал схемы pydantic) отклоняет
    ВЕСЬ пакет ДО обращения к БД — ни одно из событий пакета, включая
    валидные, не должно оказаться частично записанным."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, document_name="valid.pdf"), _event(2, total_pages=-999)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422

    session = SessionLocal()
    try:
        assert session.query(PrintJob).count() == 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 4. last_sync_at / last_contact_at / last_ingest_error
# ---------------------------------------------------------------------------


def test_last_contact_at_updates_on_success_and_partial_reject(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, printer_name="   ")]),  # business-rejected
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.last_contact_at is not None
    finally:
        session.close()


def test_last_sync_at_not_updated_when_entire_batch_is_rejected(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, printer_name="   ")]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.json()["rejected"] == 1

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.last_sync_at is None
        assert server.last_ingest_error is not None
        assert "1" in server.last_ingest_error
    finally:
        session.close()


def test_last_sync_at_not_updated_when_batch_is_partially_rejected(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, printer_name="   "), _event(2)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    body = resp.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 1

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.last_sync_at is None
    finally:
        session.close()


def test_last_sync_at_updates_when_batch_fully_accepted(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1), _event(2)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.json()["rejected"] == 0

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.last_sync_at is not None
        assert server.last_ingest_error is None
    finally:
        session.close()


def test_last_sync_at_updates_on_duplicates_only_batch(http_client, monkeypatch):
    """duplicate — не ошибка; пакет из одних дубликатов тоже "успешно
    синхронизирован" (центру просто уже нечего было добавлять)."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    session = SessionLocal()
    server = session.get(PrintServer, reg.server_id)
    server.last_sync_at = None
    session.commit()
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),  # тот же record_id -> duplicate
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.json()["duplicates"] == 1
    assert resp.json()["rejected"] == 0

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.last_sync_at is not None
    finally:
        session.close()


def test_last_ingest_error_cleared_after_a_subsequent_fully_accepted_batch(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, printer_name="   ")]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    session = SessionLocal()
    assert session.get(PrintServer, reg.server_id).last_ingest_error is not None
    session.close()

    http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(2)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    session = SessionLocal()
    try:
        assert session.get(PrintServer, reg.server_id).last_ingest_error is None
    finally:
        session.close()


def test_heartbeat_updates_last_contact_at(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    http_client.post(
        "/api/v1/agent/heartbeat",
        json=_heartbeat_payload(reg),
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.last_contact_at is not None
    finally:
        session.close()


def test_heartbeat_stores_pending_and_failed_queue_sizes_separately(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    http_client.post(
        "/api/v1/agent/heartbeat",
        json=_heartbeat_payload(reg, pending_queue_size=3, failed_queue_size=2),
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        server = session.get(PrintServer, reg.server_id)
        assert server.pending_queue_size == 3
        assert server.failed_queue_size == 2
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 5. HTTPS за реверс-прокси
# ---------------------------------------------------------------------------


def test_plain_http_rejected_when_https_required_and_no_trusted_proxy(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("APP_MODE", "central")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "true")
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 400


def test_forwarded_proto_honored_from_trusted_proxy(http_client, monkeypatch):
    """TestClient's peer is always 'testclient' (Starlette TestClient
    default scope["client"]) — trusting exactly that value simulates "the
    request came through our reverse proxy"."""
    from printaudit.database import SessionLocal

    monkeypatch.setenv("APP_MODE", "central")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "true")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "testclient")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}", "X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200


def test_forwarded_proto_ignored_from_untrusted_proxy(http_client, monkeypatch):
    """Тот же заголовок X-Forwarded-Proto: https, но TRUSTED_PROXY_IPS не
    включает peer'а TestClient — заголовок должен быть полностью
    проигнорирован (иначе любой клиент мог бы подделать 'https')."""
    from printaudit.database import SessionLocal

    monkeypatch.setenv("APP_MODE", "central")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "true")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "203.0.113.5")  # не 'testclient'
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}", "X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 400


def test_forwarded_proto_ignored_when_trusted_proxy_ips_unset(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("APP_MODE", "central")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "true")
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}", "X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 400


def test_forwarded_proto_from_trusted_proxy_does_not_bypass_other_checks(http_client, monkeypatch):
    """Доверенный прокси решает только вопрос "было ли HTTPS" — токен и
    APP_MODE=central по-прежнему проверяются как обычно."""
    from printaudit.database import SessionLocal

    monkeypatch.setenv("APP_MODE", "standalone")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "true")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "testclient")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}", "X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 404  # APP_MODE != central по-прежнему рулит


def test_admin_print_servers_page_shows_pending_and_failed_separately(http_client):
    """Смок-тест шаблона: колонки «Ждёт отправки» и «Отклонено» показаны
    раздельно, а не одним общим числом."""
    login_as(http_client, role="admin")
    resp = http_client.get("/admin/print-servers")
    assert resp.status_code == 200
    assert "Ждёт отправки" in resp.text
    assert "Отклонено" in resp.text
    assert "Последний контакт" in resp.text
