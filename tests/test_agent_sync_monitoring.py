"""Тесты синхронизации мониторинговых данных площадка -> центр
(collector/agent_sync.py::sync_monitoring_data): курсоры двигаются только
после успеха, отсутствие новых данных — no-op без сетевого вызова, сбой
сети не двигает курсор (повтор в следующем запуске)."""
from datetime import datetime, timezone


def _enable_agent_mode(monkeypatch):
    monkeypatch.setenv("APP_MODE", "agent")
    monkeypatch.setenv("CENTRAL_BASE_URL", "https://central.example.local")
    monkeypatch.setenv("AGENT_SITE_UUID", "site-uuid-1")
    monkeypatch.setenv("AGENT_PRINT_SERVER_UUID", "server-uuid-1")
    monkeypatch.setenv("AGENT_TOKEN", "test-agent-token")


def _make_device_with_health_sample(session):
    """Возвращает (site_id, device_uuid) — простые значения, не ORM-объекты,
    т.к. вызывающий тест почти всегда закрывает сессию сразу после."""
    from printaudit.models import AppUser, PrinterHealthSample
    from printaudit.monitoring import MONITORING_SOURCE_SNMP
    from printaudit.monitoring.devices import create_device, set_monitoring_source
    from printaudit.sites import get_or_create_local_print_server

    local_server = get_or_create_local_print_server(session)
    site = local_server.site
    actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
    session.add(actor)
    session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="D1")
    set_monitoring_source(session, actor=actor, device=device, source=MONITORING_SOURCE_SNMP)
    session.add(
        PrinterHealthSample(
            printer_device_id=device.id, collected_at=datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0),
            source=MONITORING_SOURCE_SNMP, is_reachable=True, device_status="online",
        )
    )
    session.commit()
    return site.id, device.uuid


def test_sync_does_nothing_when_no_new_data(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    from printaudit.config import get_settings
    from printaudit.database import SessionLocal

    calls = []
    monkeypatch.setattr(agent_sync, "send_monitoring_batch", lambda *a, **k: calls.append(1))

    session = SessionLocal()
    log = agent_sync.setup_logging(get_settings().log_dir)
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), get_settings(), log)
    session.close()

    assert calls == []


def test_sync_sends_new_health_sample_and_advances_cursor(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    from printaudit.config import get_settings
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringSyncState

    session = SessionLocal()
    site_id, device_uuid = _make_device_with_health_sample(session)
    session.close()

    captured = {}

    def _fake_send(client, base_url, token, timeout, payload):
        captured["payload"] = payload
        return {
            "devices_upserted": 1, "health_accepted": 1, "health_duplicates": 0,
            "counter_accepted": 0, "counter_duplicates": 0, "supply_accepted": 0, "supply_duplicates": 0,
            "alerts_accepted": 0, "rejected": 0,
        }

    monkeypatch.setattr(agent_sync, "send_monitoring_batch", _fake_send)

    session = SessionLocal()
    log = agent_sync.setup_logging(get_settings().log_dir)
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), get_settings(), log)
    session.close()

    assert len(captured["payload"]["health_samples"]) == 1
    assert captured["payload"]["health_samples"][0]["device_uuid"] == device_uuid
    assert captured["payload"]["devices"][0]["device_uuid"] == device_uuid

    session = SessionLocal()
    try:
        state = session.get(MonitoringSyncState, site_id)
        assert state.last_health_sample_id > 0
        assert state.last_success_at is not None
        assert state.last_error is None
    finally:
        session.close()


def test_sync_does_not_resend_already_synced_sample(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    from printaudit.config import get_settings
    from printaudit.database import SessionLocal

    session = SessionLocal()
    _make_device_with_health_sample(session)
    session.close()

    call_count = {"n": 0}

    def _fake_send(client, base_url, token, timeout, payload):
        call_count["n"] += 1
        return {
            "devices_upserted": 1, "health_accepted": 1, "health_duplicates": 0,
            "counter_accepted": 0, "counter_duplicates": 0, "supply_accepted": 0, "supply_duplicates": 0,
            "alerts_accepted": 0, "rejected": 0,
        }

    monkeypatch.setattr(agent_sync, "send_monitoring_batch", _fake_send)

    settings = get_settings()
    session = SessionLocal()
    log = agent_sync.setup_logging(settings.log_dir)
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), settings, log)
    session.close()

    session = SessionLocal()
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), settings, log)
    session.close()

    assert call_count["n"] == 1  # второй запуск не нашёл новых данных -- сеть не трогал


def test_sync_network_failure_does_not_advance_cursor(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    from printaudit.config import get_settings
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringSyncState

    session = SessionLocal()
    site_id, device_uuid = _make_device_with_health_sample(session)
    session.close()

    def _raise(*args, **kwargs):
        raise agent_sync.AgentSyncError("центр недоступен")

    monkeypatch.setattr(agent_sync, "send_monitoring_batch", _raise)

    settings = get_settings()
    session = SessionLocal()
    log = agent_sync.setup_logging(settings.log_dir)
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), settings, log)
    session.close()

    session = SessionLocal()
    try:
        state = session.get(MonitoringSyncState, site_id)
        assert state.last_health_sample_id == 0
        assert state.last_error is not None
    finally:
        session.close()


def test_run_once_does_not_crash_when_monitoring_sync_fails(app_env, monkeypatch):
    """Сбой мониторинговой синхронизации не должен ронять run_once() целиком
    (outbox заданий печати -- отдельная забота)."""
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync

    def _explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_sync, "sync_monitoring_data", _explode)
    monkeypatch.setattr(agent_sync, "send_heartbeat", lambda *a, **k: {"ok": True, "server_status": "online"})

    agent_sync.run_once()  # не должно бросить исключение
