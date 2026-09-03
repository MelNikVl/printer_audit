"""Тесты printaudit.monitoring.ingest: идемпотентная запись сэмплов,
классификация уровня расходника, и примирение алертов (открыть/оставить/
закрыть/переоткрыть) без нарушения уникальности."""
from datetime import datetime, timezone

from printaudit.monitoring.normalize import NormalizedAlertReading, NormalizedDeviceReading, NormalizedSupplyReading


def _make_device(session, site_code="SITE-A"):
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_site
    from printaudit.models import AppUser

    site = get_or_create_site(session, site_code, name=site_code)
    actor = session.query(AppUser).filter_by(login_normalized="domain\\actor").first()
    if actor is None:
        actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
        session.add(actor)
        session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="HP LaserJet")
    session.commit()
    return device


def test_health_sample_ingest_is_idempotent(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterHealthSample
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    reading = NormalizedDeviceReading(
        collected_at=datetime.now(timezone.utc), source="direct_snmp",
        is_reachable=True, device_status="online",
    )
    ingest_reading(session, device, reading)
    session.commit()
    ingest_reading(session, device, reading)  # тот же collected_at/source
    session.commit()

    assert session.query(PrinterHealthSample).filter_by(printer_device_id=device.id).count() == 1
    session.close()


def test_counter_sample_only_written_when_present(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterCounterSample
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    reading = NormalizedDeviceReading(
        collected_at=datetime.now(timezone.utc), source="direct_snmp", device_status="online",
    )
    ingest_reading(session, device, reading)
    session.commit()
    assert session.query(PrinterCounterSample).count() == 0

    reading2 = NormalizedDeviceReading(
        collected_at=datetime.now(timezone.utc) .replace(minute=(datetime.now(timezone.utc).minute + 1) % 60),
        source="direct_snmp", device_status="online", total_pages=1234,
    )
    ingest_reading(session, device, reading2)
    session.commit()
    counter = session.query(PrinterCounterSample).one()
    assert counter.total_pages == 1234
    assert counter.color_pages is None
    session.close()


def test_supply_sample_unsupported_oid_is_unknown_not_zero(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplySample
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    reading = NormalizedDeviceReading(
        collected_at=datetime.now(timezone.utc), source="direct_snmp", device_status="online",
        supplies=[NormalizedSupplyReading(supply_type="toner_black", level_percent=None)],
    )
    ingest_reading(session, device, reading)
    session.commit()

    sample = session.query(PrinterSupplySample).one()
    assert sample.level_percent is None
    assert sample.level_status == "unknown"
    session.close()


def test_supply_sample_level_status_derived_when_not_provided(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplySample
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    reading = NormalizedDeviceReading(
        collected_at=datetime.now(timezone.utc), source="direct_snmp", device_status="online",
        supplies=[NormalizedSupplyReading(supply_type="toner_black", level_percent=3.0)],
    )
    ingest_reading(session, device, reading)
    session.commit()

    sample = session.query(PrinterSupplySample).one()
    assert sample.level_status == "critical"
    session.close()


def test_device_last_seen_and_status_updated(app_env):
    from printaudit.database import SessionLocal
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    reading = NormalizedDeviceReading(
        collected_at=datetime.now(timezone.utc), source="direct_snmp", device_status="warning",
    )
    ingest_reading(session, device, reading)
    session.commit()

    session.refresh(device)
    assert device.last_status == "warning"
    assert device.last_seen_at is not None
    session.close()


def test_alert_opens_stays_open_and_resolves_when_absent(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    now = datetime.now(timezone.utc)

    reading1 = NormalizedDeviceReading(
        collected_at=now, source="direct_snmp", device_status="error",
        alerts=[NormalizedAlertReading(alert_type="paper_jam", severity="critical", external_id="paper_jam")],
    )
    ingest_reading(session, device, reading1)
    session.commit()
    alert = session.query(PrinterAlert).one()
    assert alert.resolved_at is None
    opened_at_first = alert.opened_at

    # Тот же алерт снова присутствует в следующем опросе -- не должно
    # создавать вторую строку и не должно переоткрывать (opened_at не меняется).
    reading2 = NormalizedDeviceReading(
        collected_at=now.replace(minute=(now.minute + 1) % 60), source="direct_snmp", device_status="error",
        alerts=[NormalizedAlertReading(alert_type="paper_jam", severity="critical", external_id="paper_jam")],
    )
    ingest_reading(session, device, reading2)
    session.commit()
    assert session.query(PrinterAlert).count() == 1
    still_open = session.query(PrinterAlert).one()
    assert still_open.resolved_at is None
    assert still_open.opened_at == opened_at_first

    # Алерт исчез из третьего опроса -> должен закрыться.
    reading3 = NormalizedDeviceReading(
        collected_at=now.replace(minute=(now.minute + 2) % 60), source="direct_snmp", device_status="online",
        alerts=[],
    )
    ingest_reading(session, device, reading3)
    session.commit()
    resolved = session.query(PrinterAlert).one()
    assert resolved.resolved_at is not None
    session.close()


def test_alert_reopens_without_violating_uniqueness(app_env):
    """direct_snmp adapter'ы передают external_id=alert_type (стабильный) —
    закрытая, а затем СНОВА возникшая проблема того же типа должна
    переоткрыть ту же строку, а не упасть на UNIQUE constraint."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    now = datetime.now(timezone.utc)

    def _minute_offset(n):
        return now.replace(minute=(now.minute + n) % 60)

    ingest_reading(
        session, device,
        NormalizedDeviceReading(
            collected_at=_minute_offset(0), source="direct_snmp", device_status="error",
            alerts=[NormalizedAlertReading(alert_type="cover_open", external_id="cover_open")],
        ),
    )
    session.commit()

    ingest_reading(
        session, device,
        NormalizedDeviceReading(collected_at=_minute_offset(1), source="direct_snmp", device_status="online", alerts=[]),
    )
    session.commit()
    assert session.query(PrinterAlert).one().resolved_at is not None

    # Не должно бросить IntegrityError.
    ingest_reading(
        session, device,
        NormalizedDeviceReading(
            collected_at=_minute_offset(2), source="direct_snmp", device_status="error",
            alerts=[NormalizedAlertReading(alert_type="cover_open", external_id="cover_open")],
        ),
    )
    session.commit()

    assert session.query(PrinterAlert).count() == 1
    reopened = session.query(PrinterAlert).one()
    assert reopened.resolved_at is None
    session.close()


def test_alerts_from_different_sources_do_not_close_each_other(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert
    from printaudit.monitoring.ingest import ingest_reading

    session = SessionLocal()
    device = _make_device(session)
    now = datetime.now(timezone.utc)

    ingest_reading(
        session, device,
        NormalizedDeviceReading(
            collected_at=now, source="direct_snmp", device_status="error",
            alerts=[NormalizedAlertReading(alert_type="paper_jam", external_id="paper_jam")],
        ),
    )
    session.commit()

    # zabbix_api опрос того же устройства без этой проблемы не должен
    # закрыть alert, открытый direct_snmp.
    ingest_reading(
        session, device,
        NormalizedDeviceReading(collected_at=now, source="zabbix_api", device_status="online", alerts=[]),
    )
    session.commit()

    snmp_alert = session.query(PrinterAlert).filter_by(source="direct_snmp").one()
    assert snmp_alert.resolved_at is None
    session.close()
