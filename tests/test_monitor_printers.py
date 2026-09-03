"""Тесты collector/monitor_printers.py: опрашиваются только устройства с
monitoring_source=zabbix_api/direct_snmp этой площадки, одна неудача не
проваливает весь прогон, monitoring_runs фиксируется корректно, отсутствие
Zabbix credentials не роняет скрипт."""
from printaudit.monitoring import MONITORING_SOURCE_DISABLED, MONITORING_SOURCE_MANUAL, MONITORING_SOURCE_SNMP, MONITORING_SOURCE_ZABBIX


def _make_app_user(session):
    from printaudit.models import AppUser

    user = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
    session.add(user)
    session.flush()
    return user


def _make_device(session, site, actor, source, **kwargs):
    from printaudit.monitoring.devices import create_device, set_monitoring_source

    device = create_device(session, actor=actor, site_id=site.id, display_name=kwargs.pop("display_name", "Dev"))
    set_monitoring_source(session, actor=actor, device=device, source=source, **kwargs)
    session.commit()
    return device


def test_only_zabbix_and_snmp_devices_are_polled(app_env, monkeypatch):
    import collector.monitor_printers as mp
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringRun
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "TEST", name="TEST")
    actor = _make_app_user(session)
    session.commit()

    zbx_device = _make_device(session, site, actor, MONITORING_SOURCE_ZABBIX, zabbix_host_id="10")
    _make_device(session, site, actor, MONITORING_SOURCE_MANUAL)
    _make_device(session, site, actor, MONITORING_SOURCE_DISABLED)
    session.close()

    def _fake_client_items(method, params):
        if method == "item.get":
            return [{"key_": "printer.pages.total", "lastvalue": "10"}]
        return []

    from printaudit.monitoring.zabbix_adapter import ZabbixClient

    fake_client = ZabbixClient("https://zbx", "tok", transport=_fake_client_items)
    mp.run_once(zabbix_client=fake_client)

    session = SessionLocal()
    try:
        runs = session.query(MonitoringRun).all()
        assert len(runs) == 1
        assert runs[0].source == MONITORING_SOURCE_ZABBIX
        assert runs[0].devices_polled == 1
        assert runs[0].devices_ok == 1
        assert runs[0].status == "success"
    finally:
        session.close()


def test_snmp_devices_polled_with_injected_getter(app_env):
    import collector.monitor_printers as mp
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringRun, PrinterHealthSample
    from printaudit.monitoring.snmp_adapter import DEFAULT_OIDS
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "TEST", name="TEST")
    actor = _make_app_user(session)
    session.commit()
    device = _make_device(session, site, actor, MONITORING_SOURCE_SNMP)
    device.ip_address = "10.0.0.9"
    session.commit()
    session.close()

    def _fake_getter(host, port, community, oid, timeout, retries):
        if oid == DEFAULT_OIDS["device_status"]:
            return "3"
        return None

    mp.run_once(snmp_getter=_fake_getter)

    session = SessionLocal()
    try:
        run = session.query(MonitoringRun).filter_by(source=MONITORING_SOURCE_SNMP).one()
        assert run.devices_ok == 1
        assert session.query(PrinterHealthSample).count() == 1
    finally:
        session.close()


def test_one_failing_device_does_not_stop_the_rest(app_env):
    import collector.monitor_printers as mp
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringRun
    from printaudit.monitoring.snmp_adapter import DEFAULT_OIDS
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "TEST", name="TEST")
    actor = _make_app_user(session)
    session.commit()
    good = _make_device(session, site, actor, MONITORING_SOURCE_SNMP, display_name="Good")
    good.ip_address = "10.0.0.1"
    bad = _make_device(session, site, actor, MONITORING_SOURCE_SNMP, display_name="Bad")
    bad.ip_address = "10.0.0.2"
    session.commit()
    bad_id = bad.id
    session.close()

    def _fake_getter(host, port, community, oid, timeout, retries):
        if host == "10.0.0.2":
            raise RuntimeError("boom")
        if oid == DEFAULT_OIDS["device_status"]:
            return "3"
        return None

    mp.run_once(snmp_getter=_fake_getter)

    session = SessionLocal()
    try:
        run = session.query(MonitoringRun).filter_by(source=MONITORING_SOURCE_SNMP).one()
        assert run.devices_polled == 2
        # "bad" ещё и timeout'ится на самом статусе -> is_reachable=False,
        # но это не то же самое, что провал ВСЕГО прогона -- прогон
        # завершается success, просто для этого устройства всё unknown.
        assert run.status == "success"
    finally:
        session.close()


def test_missing_zabbix_credentials_logs_failed_run_without_crashing(app_env, monkeypatch):
    import collector.monitor_printers as mp
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringRun
    from printaudit.sites import get_or_create_site

    monkeypatch.delenv("ZABBIX_API_URL", raising=False)
    monkeypatch.delenv("ZABBIX_API_TOKEN", raising=False)

    session = SessionLocal()
    site = get_or_create_site(session, "TEST", name="TEST")
    actor = _make_app_user(session)
    session.commit()
    _make_device(session, site, actor, MONITORING_SOURCE_ZABBIX, zabbix_host_id="1")
    session.close()

    mp.run_once()  # не должно бросить исключение

    session = SessionLocal()
    try:
        run = session.query(MonitoringRun).filter_by(source=MONITORING_SOURCE_ZABBIX).one()
        assert run.status == "failed"
        assert "ZABBIX_API_URL" in run.error_message
    finally:
        session.close()


def test_no_monitored_devices_does_nothing(app_env):
    import collector.monitor_printers as mp
    from printaudit.database import SessionLocal
    from printaudit.models import MonitoringRun

    mp.run_once()

    session = SessionLocal()
    try:
        assert session.query(MonitoringRun).count() == 0
    finally:
        session.close()
