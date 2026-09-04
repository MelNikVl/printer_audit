"""Сквозной сценарий Part 10: захват события на этом ПК (фейковый
PowerShell) -> локальная durable очередь -> отправка на РЕАЛЬНЫЙ
webapp/endpoint_api.py (через TestClient, не мок) -> PrintJob учтён ровно
один раз, задание с сетевой очереди не задвоено, потеря сети -> накопление
в outbox -> повторное подключение доставляет без дублей."""
import json

import pytest

from tests.conftest import login_as


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


def _register_endpoint_agent(session, hostname="PC-E2E"):
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
    return agent.uuid, token


def _client_transport(http_client, base_url):
    from endpoint_agent.sync_client import HttpResult

    def transport(url, headers, payload, timeout):
        path = url.replace(base_url, "")
        resp = http_client.post(path, json=payload, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return HttpResult(resp.status_code, body)

    return transport


def _event(record_id, printer_name):
    props = [None] * 9
    props[0] = str(record_id)
    props[1] = "report.docx"
    props[2] = "DOMAIN\\ivanov"
    props[3] = "PC-E2E"
    props[4] = printer_name
    props[8] = "3"
    return {"RecordId": record_id, "TimeCreated": "2026-09-04T09:00:00.000Z", "Properties": props}


def _port_map_json():
    return json.dumps(
        [
            {"Name": "USB-HP-LaserJet", "PortName": "USB001", "Type": "Local"},
            {"Name": "SharedNetworkPrinter", "PortName": "SharedNetworkPrinter", "Type": "Connection"},
        ]
    )


def test_usb_job_counted_once_network_queue_excluded_survives_network_loss(http_client, monkeypatch):
    """Полный сценарий: 2 события на этом ПК (одно с USB-принтера, одно с
    сетевой очереди Print Server) -> захват -> первая попытка отправки
    падает по сети (outbox копится) -> вторая попытка успешна -> ровно один
    PrintJob (USB), без дублей при повторном цикле."""
    from endpoint_agent import outbox
    from endpoint_agent.config import EndpointAgentConfig
    from endpoint_agent.runner import capture_cycle, sync_cycle
    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")

    session = SessionLocal()
    endpoint_uuid, token = _register_endpoint_agent(session)
    session.close()

    base_url = "https://site.local"
    cfg = EndpointAgentConfig(server_base_url=base_url, token=token, endpoint_uuid=endpoint_uuid, hostname="PC-E2E")

    import tempfile
    from pathlib import Path

    conn = outbox.open_db(Path(tempfile.mkdtemp()) / "outbox.sqlite3")
    log = _Log()

    events_json = json.dumps([_event(1, "USB-HP-LaserJet"), _event(2, "SharedNetworkPrinter")])
    inserted = capture_cycle(
        cfg, conn, log, event_runner=lambda args: events_json, port_runner=lambda args: _port_map_json(),
    )
    assert inserted == 1  # сетевая очередь исключена -- не задвоение с Print Server

    def failing_transport(url, headers, payload, timeout):
        from endpoint_agent.sync_client import SyncError

        raise SyncError("симуляция обрыва сети")

    sync_cycle(cfg, conn, log, transport=failing_transport)
    assert outbox.pending_count(conn) == 1  # накопилось, не потеряно

    session = SessionLocal()
    try:
        assert session.query(PrintJob).count() == 0
    finally:
        session.close()

    real_transport = _client_transport(http_client, base_url)
    # Backoff после сбоя откладывает повтор на будущее -- подтверждаем это,
    # затем форсируем немедленную повторную попытку (то, что произошло бы
    # естественно при следующем цикле после восстановления сети).
    assert outbox.fetch_due_batch(conn, 10) == []
    conn.execute("UPDATE outbox_events SET next_attempt_at = NULL")
    conn.commit()

    sync_cycle(cfg, conn, log, transport=real_transport)
    assert outbox.pending_count(conn) == 0
    assert outbox.failed_count(conn) == 0

    session = SessionLocal()
    try:
        jobs = session.query(PrintJob).all()
        assert len(jobs) == 1
        assert jobs[0].printer_name == "USB-HP-LaserJet"
        assert jobs[0].endpoint_agent_id is not None
        assert jobs[0].print_server_id is None
    finally:
        session.close()

    # Повторный цикл (например, второй запуск задачи) НЕ создаёт дублей --
    # курсор уже продвинут, повторной отправки нет вообще.
    inserted_again = capture_cycle(
        cfg, conn, log, event_runner=lambda args: events_json, port_runner=lambda args: _port_map_json(),
    )
    assert inserted_again == 0
    sync_cycle(cfg, conn, log, transport=real_transport)
    session = SessionLocal()
    try:
        assert session.query(PrintJob).count() == 1
    finally:
        session.close()
