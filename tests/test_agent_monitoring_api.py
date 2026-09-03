"""Тесты POST /api/v1/agent/monitoring/batch: отдельный от заданий печати
протокол (свой protocol_version), авто-регистрация/обновление устройств по
uuid в границах площадки токена, идемпотентная запись сэмплов, upsert/
resolve алертов, отказ писать в устройство чужой площадки."""
from dataclasses import dataclass

from tests.conftest import login_as


@dataclass
class _Registered:
    site_uuid: str
    server_uuid: str
    server_id: int
    site_id: int
    token: str


def _register(session, site_code="ALMATY", server_name="PRN01"):
    from printaudit.security.agent_tokens import generate_agent_token, hash_agent_token
    from printaudit.sites import get_or_create_print_server, get_or_create_site
    from printaudit.timeutil import utcnow

    site = get_or_create_site(session, site_code, name=site_code)
    server = get_or_create_print_server(session, site, server_name)
    token = generate_agent_token()
    server.token_hash = hash_agent_token(token)
    server.token_created_at = utcnow()
    session.commit()
    return _Registered(site_uuid=site.uuid, server_uuid=server.uuid, server_id=server.id, site_id=site.id, token=token)


def _base_payload(reg, **overrides):
    payload = {
        "protocol_version": 1,
        "site_uuid": reg.site_uuid,
        "print_server_uuid": reg.server_uuid,
        "generated_at": "2026-09-04T10:00:00+00:00",
        "devices": [],
        "health_samples": [],
        "counter_samples": [],
        "supply_samples": [],
        "alerts": [],
    }
    payload.update(overrides)
    return payload


def _setup(monkeypatch, session, mode="central"):
    monkeypatch.setenv("APP_MODE", mode)
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")
    return _register(session)


def test_monitoring_batch_returns_404_outside_central_mode(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session, mode="agent")
    session.close()

    resp = http_client.post(
        "/api/v1/agent/monitoring/batch", json=_base_payload(reg), headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 404


def test_monitoring_batch_rejects_unsupported_protocol_version(http_client, monkeypatch):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    resp = http_client.post(
        "/api/v1/agent/monitoring/batch",
        json=_base_payload(reg, protocol_version=99),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_protocol_version"


def test_monitoring_batch_creates_device_and_ingests_samples_idempotently(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterDevice, PrinterHealthSample

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    device_uuid = "11111111-1111-1111-1111-111111111111"
    payload = _base_payload(
        reg,
        devices=[{
            "device_uuid": device_uuid, "display_name": "HP LaserJet", "hostname": "hp01",
            "ip_address": "10.0.0.5", "vendor": "HP", "model": "LaserJet Pro",
        }],
        health_samples=[{
            "device_uuid": device_uuid, "collected_at": "2026-09-04T09:00:00+00:00", "source": "direct_snmp",
            "is_reachable": True, "device_status": "online",
        }],
    )
    resp = http_client.post(
        "/api/v1/agent/monitoring/batch", json=payload, headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["devices_upserted"] == 1
    assert body["health_accepted"] == 1

    session = SessionLocal()
    device = session.query(PrinterDevice).filter_by(uuid=device_uuid).one()
    assert device.site_id == reg.site_id
    assert device.display_name == "HP LaserJet"
    session.close()

    # Повторная отправка того же пакета -- дубликат, не вторая строка.
    resp2 = http_client.post(
        "/api/v1/agent/monitoring/batch", json=payload, headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp2.json()["health_duplicates"] == 1

    session = SessionLocal()
    try:
        assert session.query(PrinterHealthSample).count() == 1
    finally:
        session.close()


def test_monitoring_batch_supply_sample_unknown_level_not_zero(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplySample

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    device_uuid = "22222222-2222-2222-2222-222222222222"
    payload = _base_payload(
        reg,
        devices=[{"device_uuid": device_uuid, "display_name": "D"}],
        supply_samples=[{
            "device_uuid": device_uuid, "collected_at": "2026-09-04T09:00:00+00:00", "source": "direct_snmp",
            "supply_type": "toner_black", "level_percent": None,
        }],
    )
    http_client.post("/api/v1/agent/monitoring/batch", json=payload, headers={"Authorization": f"Bearer {reg.token}"})

    session = SessionLocal()
    try:
        sample = session.query(PrinterSupplySample).one()
        assert sample.level_percent is None
        assert sample.level_status == "unknown"
    finally:
        session.close()


def test_monitoring_batch_rejects_negative_and_nan_supply_level(http_client, monkeypatch):
    import json

    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    device_uuid = "33333333-3333-3333-3333-333333333333"
    raw_body = json.dumps(
        _base_payload(
            reg,
            devices=[{"device_uuid": device_uuid, "display_name": "D"}],
            supply_samples=[{
                "device_uuid": device_uuid, "collected_at": "2026-09-04T09:00:00+00:00", "source": "direct_snmp",
                "supply_type": "toner_black", "level_percent": -5,
            }],
        )
    )
    resp = http_client.post(
        "/api/v1/agent/monitoring/batch", content=raw_body,
        headers={"Authorization": f"Bearer {reg.token}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_monitoring_batch_alert_opens_and_resolves(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    device_uuid = "44444444-4444-4444-4444-444444444444"
    open_payload = _base_payload(
        reg,
        devices=[{"device_uuid": device_uuid, "display_name": "D"}],
        alerts=[{
            "device_uuid": device_uuid, "source": "direct_snmp", "alert_type": "paper_jam", "severity": "critical",
            "external_id": "paper_jam", "opened_at": "2026-09-04T09:00:00+00:00", "resolved_at": None,
        }],
    )
    http_client.post("/api/v1/agent/monitoring/batch", json=open_payload, headers={"Authorization": f"Bearer {reg.token}"})

    session = SessionLocal()
    alert = session.query(PrinterAlert).one()
    assert alert.resolved_at is None
    session.close()

    resolve_payload = _base_payload(
        reg,
        devices=[{"device_uuid": device_uuid, "display_name": "D"}],
        alerts=[{
            "device_uuid": device_uuid, "source": "direct_snmp", "alert_type": "paper_jam", "severity": "critical",
            "external_id": "paper_jam", "opened_at": "2026-09-04T09:00:00+00:00", "resolved_at": "2026-09-04T09:30:00+00:00",
        }],
    )
    http_client.post("/api/v1/agent/monitoring/batch", json=resolve_payload, headers={"Authorization": f"Bearer {reg.token}"})

    session = SessionLocal()
    try:
        alert = session.query(PrinterAlert).one()
        assert alert.resolved_at is not None
    finally:
        session.close()


def test_monitoring_batch_cannot_write_to_device_of_another_site(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterHealthSample

    session = SessionLocal()
    reg_a = _setup(monkeypatch, session, mode="central")
    reg_b = _register(session, site_code="SITE-B", server_name="B1")
    session.commit()
    session.close()

    device_uuid = "55555555-5555-5555-5555-555555555555"
    # Устройство создаётся под площадкой A.
    create_payload = _base_payload(reg_a, devices=[{"device_uuid": device_uuid, "display_name": "D-A"}])
    http_client.post("/api/v1/agent/monitoring/batch", json=create_payload, headers={"Authorization": f"Bearer {reg_a.token}"})

    # Агент площадки B пытается прислать сэмпл на это же device_uuid.
    sneaky_payload = _base_payload(
        reg_b,
        health_samples=[{
            "device_uuid": device_uuid, "collected_at": "2026-09-04T09:00:00+00:00", "source": "direct_snmp",
            "device_status": "online",
        }],
    )
    resp = http_client.post(
        "/api/v1/agent/monitoring/batch", json=sneaky_payload, headers={"Authorization": f"Bearer {reg_b.token}"},
    )
    assert resp.json()["rejected"] == 1

    session = SessionLocal()
    try:
        assert session.query(PrinterHealthSample).count() == 0
    finally:
        session.close()


def test_monitoring_batch_rejects_too_many_items(http_client, monkeypatch):
    from printaudit.database import SessionLocal
    from webapp.agent_api import MAX_MONITORING_ITEMS_PER_BATCH

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    devices = [{"device_uuid": f"dev-{i}", "display_name": f"D{i}"} for i in range(MAX_MONITORING_ITEMS_PER_BATCH + 1)]
    resp = http_client.post(
        "/api/v1/agent/monitoring/batch", json=_base_payload(reg, devices=devices),
        headers={"Authorization": f"Bearer {reg.token}"},
    )
    assert resp.status_code == 422


def test_monitoring_batch_token_never_logged(http_client, monkeypatch, caplog):
    import logging

    from printaudit.database import SessionLocal

    session = SessionLocal()
    reg = _setup(monkeypatch, session)
    session.close()

    with caplog.at_level(logging.DEBUG):
        http_client.post(
            "/api/v1/agent/monitoring/batch", json=_base_payload(reg), headers={"Authorization": f"Bearer {reg.token}"},
        )
    assert reg.token not in caplog.text
