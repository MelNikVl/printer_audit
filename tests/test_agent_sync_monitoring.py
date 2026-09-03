"""Тесты синхронизации мониторинговых данных площадка -> центр
(collector/agent_sync.py::sync_monitoring_data): курсоры двигаются только
после успеха (в т.ч. НЕ двигаются при partial reject — см. регрессионный
тест ниже), отсутствие новых данных — no-op без сетевого вызова, сбой сети
не двигает курсор (повтор в следующем запуске), составной курсор алертов
устойчив к одинаковому updated_at у нескольких строк."""
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


def test_sync_partial_reject_does_not_advance_cursor_and_retries_next_run(app_env, monkeypatch):
    """Регрессия: central вернул HTTP 200 с rejected=1 -- ни один курсор не
    должен продвинуться (нет per-item ack'ов у /monitoring/batch, поэтому
    невозможно узнать, какой именно элемент отклонён; если бы курсор
    продвинулся, отклонённый элемент был бы потерян навсегда). Следующий
    запуск отправляет ТОТ ЖЕ пакет целиком повторно; когда central
    подтверждает rejected=0, курсор наконец продвигается."""
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    from printaudit.config import get_settings
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringSyncState

    session = SessionLocal()
    site_id, device_uuid = _make_device_with_health_sample(session)
    session.close()

    calls = []

    def _fake_send(client, base_url, token, timeout, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "devices_upserted": 1, "health_accepted": 0, "health_duplicates": 0,
                "counter_accepted": 0, "counter_duplicates": 0, "supply_accepted": 0, "supply_duplicates": 0,
                "alerts_accepted": 0, "rejected": 1,
            }
        # Повтор: центр идемпотентно определяет ту же строку как duplicate.
        return {
            "devices_upserted": 1, "health_accepted": 0, "health_duplicates": 1,
            "counter_accepted": 0, "counter_duplicates": 0, "supply_accepted": 0, "supply_duplicates": 0,
            "alerts_accepted": 0, "rejected": 0,
        }

    monkeypatch.setattr(agent_sync, "send_monitoring_batch", _fake_send)

    settings = get_settings()
    log = agent_sync.setup_logging(settings.log_dir)

    session = SessionLocal()
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), settings, log)
    session.close()

    session = SessionLocal()
    try:
        state = session.get(MonitoringSyncState, site_id)
        assert state.last_health_sample_id == 0  # курсор НЕ продвинут
        assert state.last_success_at is None
        assert state.last_error is not None and "1" in state.last_error
    finally:
        session.close()

    session = SessionLocal()
    agent_sync.sync_monitoring_data(session, agent_sync.get_agent_settings(), settings, log)
    session.close()

    assert len(calls) == 2
    # Второй вызов отправил ТУ ЖЕ строку повторно (курсор не сдвинулся).
    assert calls[0]["health_samples"][0]["device_uuid"] == device_uuid
    assert calls[1]["health_samples"][0]["device_uuid"] == device_uuid

    session = SessionLocal()
    try:
        state = session.get(MonitoringSyncState, site_id)
        assert state.last_health_sample_id > 0  # теперь продвинут
        assert state.last_success_at is not None
        assert state.last_error is None
    finally:
        session.close()


def test_alert_cursor_survives_multiple_alerts_with_identical_updated_at(app_env):
    """Регрессия: несколько алертов с ОДИНАКОВЫМ updated_at и limit меньше
    их числа -- курсор по одному updated_at пропустил бы навсегда те
    строки, что не попали в первую "страницу". Составной курсор
    (updated_at, id) должен доставить все строки без пропусков и без
    дублей за несколько вызовов _build_monitoring_payload."""
    import collector.agent_sync as agent_sync
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser, MonitoringSyncState, PrinterAlert
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_local_print_server

    session = SessionLocal()
    local_server = get_or_create_local_print_server(session)
    site = local_server.site
    actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
    session.add(actor)
    session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="D1")
    session.flush()

    same_ts = datetime(2026, 9, 5, 12, 0, 0)
    for i in range(5):
        alert = PrinterAlert(
            printer_device_id=device.id, source="direct_snmp", alert_type=f"type_{i}",
            severity="warning", opened_at=same_ts, external_id=f"type_{i}",
        )
        session.add(alert)
        session.flush()
        alert.updated_at = same_ts  # форсируем идентичный updated_at у всех строк
        session.flush()
    session.commit()

    state = MonitoringSyncState(site_id=site.id)
    session.add(state)
    session.flush()

    seen_types = []
    for _ in range(10):  # с запасом -- должно хватить 3 страниц по 2
        built = agent_sync._build_monitoring_payload(session, site, state, limit=2)
        if built is None:
            break
        body, cursors = built
        assert body["alerts"], "пустая страница при built is not None -- баг пагинации"
        seen_types.extend(a["alert_type"] for a in body["alerts"])
        state.last_alert_synced_at = cursors["alert_at"]
        state.last_alert_synced_id = cursors["alert_id"]
        session.commit()

    session.close()
    assert sorted(seen_types) == sorted(f"type_{i}" for i in range(5))  # все 5, без пропусков и дублей


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
