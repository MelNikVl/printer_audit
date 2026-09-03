"""Тесты POST /api/v1/endpoint/events/batch и /heartbeat — приём заданий
печати от endpoint-агентов на СВОЁМ сервере площадки (standalone/agent,
НЕ central), идемпотентность, применение privacy-политики/тарифа/отдела
как у любого другого print_job, отдельная (от Print Server) область
уникальности очередей, постановка в outbox только в agent-режиме."""
from dataclasses import dataclass

import pytest

from tests.conftest import login_as


@pytest.fixture(autouse=True)
def _no_https_requirement(monkeypatch):
    # Endpoint API использует ту же AGENT_REQUIRE_HTTPS настройку, что и
    # /api/v1/agent/* (см. webapp/endpoint_api.py::require_endpoint_agent) —
    # по умолчанию true, TestClient всегда шлёт обычный HTTP.
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")


@dataclass
class _Registered:
    endpoint_uuid: str
    site_id: int
    token: str


def _register_endpoint_agent(session, hostname="PC-01"):
    from printaudit.models import EndpointAgent
    from printaudit.security.agent_tokens import generate_agent_token, hash_agent_token
    from printaudit.sites import get_or_create_site
    from printaudit.timeutil import utcnow

    site = get_or_create_site(session, "TEST", name="TEST")
    agent = EndpointAgent(site_id=site.id, hostname=hostname, display_name=hostname)
    token = generate_agent_token()
    agent.token_hash = hash_agent_token(token)
    agent.token_created_at = utcnow()
    session.add(agent)
    session.commit()
    return _Registered(endpoint_uuid=agent.uuid, site_id=site.id, token=token)


def _batch_payload(reg, events, protocol_version=1, hostname="PC-01"):
    return {
        "protocol_version": protocol_version,
        "endpoint_uuid": reg.endpoint_uuid,
        "hostname": hostname,
        "agent_version": "1.0",
        "generated_at": "2026-09-04T10:00:00+00:00",
        "events": events,
    }


def _event(record_id, **overrides):
    base = {
        "record_id": record_id,
        "job_id": str(record_id),
        "time_created": "2026-09-04T09:00:00+00:00",
        "user_name": "DOMAIN\\ivanov",
        "printer_name": "USB-HP-LaserJet",
        "total_pages": 3,
    }
    base.update(overrides)
    return base


def test_endpoint_batch_available_in_standalone_mode(http_client):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    resp = http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_endpoint_batch_returns_404_in_central_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("APP_MODE", "central")
    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    resp = http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 404


def test_endpoint_batch_rejects_unsupported_protocol_version(http_client):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    resp = http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)], protocol_version=9),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_protocol_version"


def test_endpoint_batch_rejects_wrong_token(http_client):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    resp = http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_endpoint_batch_is_idempotent(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    payload = _batch_payload(reg, [_event(1)])
    http_client.post("/api/v1/endpoint/events/batch", json=payload, headers={"Authorization": f"Bearer {reg.token}"})
    resp2 = http_client.post("/api/v1/endpoint/events/batch", json=payload, headers={"Authorization": f"Bearer {reg.token}"})
    assert resp2.json()["duplicates"] == 1

    session = SessionLocal()
    try:
        assert session.query(PrintJob).count() == 1
    finally:
        session.close()


def test_endpoint_job_records_source_computer_and_endpoint_agent(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    session = SessionLocal()
    reg = _register_endpoint_agent(session, hostname="ACCOUNTING-PC-07")
    session.close()

    http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)], hostname="ACCOUNTING-PC-07"),
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        job = session.query(PrintJob).one()
        assert job.source_computer == "ACCOUNTING-PC-07"
        assert job.endpoint_agent_id is not None
        assert job.print_server_id is None  # НЕ задание Print Server
    finally:
        session.close()


def test_endpoint_document_name_policy_applied(http_client):
    from printaudit.database import SessionLocal
    from printaudit.config import get_settings
    from printaudit.models import PrintJob

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    get_settings().document_name_policy = "masked"
    http_client.post(
        "/api/v1/endpoint/events/batch",
        json=_batch_payload(reg, [_event(1, document_name="Зарплата.xlsx")]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        job = session.query(PrintJob).one()
        assert job.document_name == "•••.xlsx"
    finally:
        session.close()


def test_endpoint_printer_queue_scoped_separately_from_print_server_queue_of_same_name(http_client):
    """Требование: одноимённые локальные принтеры на разных ПК (или тот же
    printer_name, что и очередь Print Server) не должны конфликтовать/
    смешиваться."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue
    from printaudit.printers.resolver import get_or_create_printer_queue
    from printaudit.sites import get_or_create_local_print_server

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    local_server = get_or_create_local_print_server(session)
    get_or_create_printer_queue(session, "USB-HP-LaserJet", print_server_id=local_server.id)
    session.commit()
    session.close()

    http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        queues = session.query(PrinterQueue).filter_by(printer_name="USB-HP-LaserJet").all()
        assert len(queues) == 2  # одна для Print Server, одна для endpoint-агента
        assert {q.endpoint_agent_id is not None for q in queues} == {True, False}
    finally:
        session.close()


def test_endpoint_job_enqueues_outbox_only_in_agent_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    monkeypatch.setenv("APP_MODE", "standalone")
    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()
    http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    session = SessionLocal()
    try:
        assert session.query(OutboxEvent).count() == 0
    finally:
        session.close()


def test_endpoint_job_enqueues_outbox_in_agent_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    monkeypatch.setenv("APP_MODE", "agent")
    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()
    http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    session = SessionLocal()
    try:
        assert session.query(OutboxEvent).count() == 1
    finally:
        session.close()


def test_endpoint_heartbeat_updates_agent_state(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import EndpointAgent

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    http_client.post(
        "/api/v1/endpoint/heartbeat",
        json={
            "protocol_version": 1, "endpoint_uuid": reg.endpoint_uuid, "hostname": "PC-01",
            "agent_version": "1.2", "pending_queue_size": 3, "failed_queue_size": 0,
        },
        headers={"Authorization": f"Bearer {reg.token}"},
    )

    session = SessionLocal()
    try:
        agent = session.query(EndpointAgent).filter_by(uuid=reg.endpoint_uuid).one()
        assert agent.agent_version == "1.2"
        assert agent.pending_queue_size == 3
        assert agent.last_heartbeat_at is not None
    finally:
        session.close()


def test_endpoint_batch_rejects_too_many_events(http_client):
    from printaudit.database import SessionLocal
    from webapp.endpoint_api import MAX_EVENTS_PER_BATCH

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    events = [_event(i) for i in range(MAX_EVENTS_PER_BATCH + 1)]
    resp = http_client.post(
        "/api/v1/endpoint/events/batch", json=_batch_payload(reg, events), headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_endpoint_batch_rejects_oversized_body(http_client):
    from printaudit.database import SessionLocal
    from webapp.endpoint_api import MAX_BODY_BYTES

    session = SessionLocal()
    reg = _register_endpoint_agent(session)
    session.close()

    huge = "x" * (MAX_BODY_BYTES + 1000)
    raw_body = (
        '{"protocol_version":1,"endpoint_uuid":"' + reg.endpoint_uuid + '","hostname":"PC-01",'
        '"agent_version":"1.0","generated_at":"2026-09-04T10:00:00+00:00","events":[],'
        '"padding":"' + huge + '"}'
    )
    resp = http_client.post(
        "/api/v1/endpoint/events/batch", content=raw_body,
        headers={"Authorization": f"Bearer {reg.token}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_admin_endpoint_agents_page_requires_role(http_client):
    login_as(http_client, role="viewer")
    resp = http_client.get("/admin/endpoint-agents")
    assert resp.status_code == 403
