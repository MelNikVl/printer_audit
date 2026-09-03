"""Тесты центрального API для агентов (/api/v1/agent/*): аутентификация по
bearer-токену (валидный/неверный/отозванный/ротированный), идемпотентность
приёма (дубликат внутри пакета и между пакетами, частично повторяющийся
пакет), сверка site_uuid/print_server_uuid с токеном, heartbeat и
вычисляемый статус, требование HTTPS, отсутствие токена в логах/ответах."""
import logging
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
    """Возвращает только простые значения (не ORM-объекты) — вызывающий тест
    почти всегда закрывает сессию сразу после регистрации, а ORM-объекты
    после этого становятся detached."""
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


def _batch_payload(reg: _Registered, events):
    return {
        "protocol_version": 1,
        "site_uuid": reg.site_uuid,
        "print_server_uuid": reg.server_uuid,
        "generated_at": "2026-09-03T10:00:00+00:00",
        "events": events,
    }


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


def test_events_batch_requires_https_when_configured(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "true")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 400
    assert "HTTPS" in resp.json()["detail"]


def test_events_batch_rejects_missing_token(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post("/api/v1/agent/events/batch", json=_batch_payload(reg, [_event(1)]))
    assert resp.status_code == 401


def test_events_batch_rejects_wrong_token(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": "Bearer completely-wrong-token"},
    )
    assert resp.status_code == 401


def test_events_batch_rejects_disabled_server_token(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.query(PrintServer).filter_by(id=reg.server_id).update({"is_disabled": True})
    session.commit()
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 401


def test_events_batch_rejects_rotated_out_token(http_client, monkeypatch):
    """После ротации старый токен должен немедленно перестать работать —
    его хэш уже не совпадает ни с одной строкой PrintServer.token_hash."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer
    from printaudit.security.agent_tokens import generate_agent_token, hash_agent_token

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    new_token = generate_agent_token()
    session.query(PrintServer).filter_by(id=reg.server_id).update({"token_hash": hash_agent_token(new_token)})
    session.commit()
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 401

    resp2 = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1)]),
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert resp2.status_code == 200


def test_events_batch_rejects_identity_mismatch(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg_a = _register_print_server(session, site_code="SITE-A", server_name="A1")
    reg_b = _register_print_server(session, site_code="SITE-B", server_name="B1")
    session.close()

    # Токен верный (reg_a), но тело утверждает, что это reg_b.
    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg_b, [_event(1)]),
        headers={"Authorization": f"Bearer {reg_a.token}"},
    )
    assert resp.status_code == 400


def test_events_batch_inserts_and_is_idempotent_within_and_across_batches(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()
    headers = {"Authorization": f"Bearer {reg.token}"}

    # Дубликат record_id ВНУТРИ одного пакета.
    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, document_name="a.pdf"), _event(1, document_name="a.pdf")]),
        headers=headers,
    )
    body = resp.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 1

    # Повторная отправка ТОГО ЖЕ пакета целиком — не должно быть новых вставок.
    resp2 = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, document_name="a.pdf"), _event(1, document_name="a.pdf")]),
        headers=headers,
    )
    body2 = resp2.json()
    assert body2["accepted"] == 0
    assert body2["duplicates"] == 2

    # Частично повторяющийся пакет: record_id=1 уже есть, record_id=2 новый.
    resp3 = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1), _event(2, document_name="b.pdf")]),
        headers=headers,
    )
    body3 = resp3.json()
    assert body3["accepted"] == 1
    assert body3["duplicates"] == 1

    session = SessionLocal()
    try:
        assert session.query(PrintJob).filter_by(print_server_id=reg.server_id).count() == 2
    finally:
        session.close()


def test_events_batch_same_record_id_on_two_different_servers_both_accepted(http_client, monkeypatch):
    """Требование: одинаковый record_id с двух РАЗНЫХ Print Server не
    конфликтует — идемпотентность построена на (print_server_id, record_id),
    не на голом record_id."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg_a = _register_print_server(session, site_code="SITE-A", server_name="A1")
    reg_b = _register_print_server(session, site_code="SITE-A", server_name="A2")
    session.close()

    resp_a = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg_a, [_event(42, document_name="from-a.pdf")]),
        headers={"Authorization": f"Bearer {reg_a.token}"},
    )
    resp_b = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg_b, [_event(42, document_name="from-b.pdf")]),
        headers={"Authorization": f"Bearer {reg_b.token}"},
    )
    assert resp_a.json()["accepted"] == 1
    assert resp_b.json()["accepted"] == 1

    session = SessionLocal()
    try:
        docs = {j.document_name for j in session.query(PrintJob).filter_by(record_id=42).all()}
        assert docs == {"from-a.pdf", "from-b.pdf"}
    finally:
        session.close()


def test_events_batch_same_printer_name_on_two_different_servers_both_kept(http_client, monkeypatch):
    """Требование: одинаковое имя очереди на разных серверах не является
    одной и той же PrinterQueue."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg_a = _register_print_server(session, site_code="SITE-A", server_name="A1")
    reg_b = _register_print_server(session, site_code="SITE-B", server_name="B1")
    session.close()

    for reg, record_id in ((reg_a, 1), (reg_b, 2)):
        http_client.post(
            "/api/v1/agent/events/batch",
            json=_batch_payload(reg, [_event(record_id, printer_name="HP-SHARED")]),
            headers={"Authorization": f"Bearer {reg.token}"},
        )

    session = SessionLocal()
    try:
        queues = session.query(PrinterQueue).filter_by(printer_name="HP-SHARED").all()
        assert len(queues) == 2
        assert {q.print_server_id for q in queues} == {reg_a.server_id, reg_b.server_id}
    finally:
        session.close()


def test_events_batch_rejects_invalid_event_without_failing_whole_batch(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/events/batch",
        json=_batch_payload(reg, [_event(1, total_pages=-5), _event(2, document_name="ok.pdf")]),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    body = resp.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 1
    statuses = {r["record_id"]: r["status"] for r in body["results"]}
    assert statuses[1] == "rejected"
    assert statuses[2] == "inserted"


def test_agent_token_never_appears_in_logs_or_responses(http_client, monkeypatch, caplog):
    from printaudit.database import SessionLocal

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    with caplog.at_level(logging.DEBUG):
        resp = http_client.post(
            "/api/v1/agent/events/batch",
            json=_batch_payload(reg, [_event(1, total_pages=-1)]),
            headers={"Authorization": f"Bearer {reg.token}"},
        )
    assert reg.token not in resp.text
    assert reg.token not in caplog.text


def test_heartbeat_updates_server_and_returns_computed_status(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrintServer

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    session = SessionLocal()
    reg = _register_print_server(session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/heartbeat",
        json={
            "protocol_version": 1, "site_uuid": reg.site_uuid, "print_server_uuid": reg.server_uuid,
            "agent_version": "1.2.3", "pending_queue_size": 7, "last_error": None,
        },
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["server_status"] == "online"

    session = SessionLocal()
    try:
        updated = session.get(PrintServer, reg.server_id)
        assert updated.agent_version == "1.2.3"
        assert updated.pending_queue_size == 7
        assert updated.last_heartbeat_at is not None
    finally:
        session.close()


def test_heartbeat_offline_status_for_stale_server():
    from datetime import datetime, timedelta, timezone

    from printaudit.sites import compute_status

    class _Stale:
        is_disabled = False
        last_heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=2)

    assert compute_status(_Stale()) == "offline"


def test_admin_print_servers_page_requires_role(http_client):
    login_as(http_client, role="viewer")
    resp = http_client.get("/admin/print-servers")
    assert resp.status_code == 403
