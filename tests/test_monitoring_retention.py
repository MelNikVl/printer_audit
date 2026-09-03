"""Тесты printaudit.monitoring.retention: агрегация уровня расходника в
дневной тренд (идемпотентно), очистка сырых сэмплов старше окна, активные
алерты никогда не удаляются, решённые — только по истечении отдельного
(более длинного) срока хранения, и что run_retention сохраняет тренд ДО
удаления сырых данных."""
from datetime import datetime, timedelta, timezone


def _make_device(session):
    from printaudit.models import AppUser
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_site

    site = get_or_create_site(session, "TEST", name="TEST")
    actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
    session.add(actor)
    session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="D1")
    session.commit()
    return device


def _add_supply_sample(session, device, days_ago, level, supply_type="toner_black", source="direct_snmp", minute_offset=0):
    from printaudit.models import PrinterSupplySample
    from printaudit.monitoring import classify_supply_level

    collected_at = (
        datetime.now(timezone.utc) - timedelta(days=days_ago) + timedelta(minutes=minute_offset)
    ).replace(tzinfo=None, second=0, microsecond=0)
    session.add(
        PrinterSupplySample(
            printer_device_id=device.id, collected_at=collected_at, source=source, supply_type=supply_type,
            level_percent=level, level_status=classify_supply_level(level),
        )
    )


def test_aggregate_computes_min_avg_max_per_day(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplyDailyAgg
    from printaudit.monitoring.retention import RAW_RETENTION_DAYS, aggregate_supply_samples_to_daily

    session = SessionLocal()
    device = _make_device(session)
    # Три сэмпла в один и тот же (старый) день.
    old_day = RAW_RETENTION_DAYS + 5
    _add_supply_sample(session, device, old_day, 50, minute_offset=0)
    _add_supply_sample(session, device, old_day, 30, minute_offset=5)
    _add_supply_sample(session, device, old_day, 70, minute_offset=10)
    session.commit()

    written = aggregate_supply_samples_to_daily(session)
    session.commit()
    assert written == 1

    agg = session.query(PrinterSupplyDailyAgg).one()
    assert agg.min_level_percent == 30
    assert agg.max_level_percent == 70
    assert agg.avg_level_percent == 50
    assert agg.sample_count == 3
    session.close()


def test_aggregate_is_idempotent(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplyDailyAgg
    from printaudit.monitoring.retention import RAW_RETENTION_DAYS, aggregate_supply_samples_to_daily

    session = SessionLocal()
    device = _make_device(session)
    _add_supply_sample(session, device, RAW_RETENTION_DAYS + 1, 40)
    session.commit()

    aggregate_supply_samples_to_daily(session)
    session.commit()
    aggregate_supply_samples_to_daily(session)  # повторный вызов на тех же данных
    session.commit()

    assert session.query(PrinterSupplyDailyAgg).count() == 1
    session.close()


def test_aggregate_ignores_unknown_level(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplyDailyAgg
    from printaudit.monitoring.retention import RAW_RETENTION_DAYS, aggregate_supply_samples_to_daily

    session = SessionLocal()
    device = _make_device(session)
    _add_supply_sample(session, device, RAW_RETENTION_DAYS + 1, None)
    session.commit()

    aggregate_supply_samples_to_daily(session)
    session.commit()
    assert session.query(PrinterSupplyDailyAgg).count() == 0
    session.close()


def test_purge_raw_samples_only_removes_old_rows(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplySample
    from printaudit.monitoring.retention import RAW_RETENTION_DAYS, purge_raw_samples

    session = SessionLocal()
    device = _make_device(session)
    _add_supply_sample(session, device, RAW_RETENTION_DAYS + 5, 50)  # старый -- удалится
    _add_supply_sample(session, device, 1, 60)  # свежий -- останется
    session.commit()

    purge_raw_samples(session)
    session.commit()

    remaining = session.query(PrinterSupplySample).all()
    assert len(remaining) == 1
    assert remaining[0].level_percent == 60
    session.close()


def test_active_alerts_are_never_purged_regardless_of_age(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert
    from printaudit.monitoring.retention import purge_resolved_alerts

    session = SessionLocal()
    device = _make_device(session)
    session.add(
        PrinterAlert(
            printer_device_id=device.id, source="direct_snmp", alert_type="paper_jam", severity="critical",
            opened_at=datetime.now(timezone.utc) - timedelta(days=400), external_id="paper_jam", resolved_at=None,
        )
    )
    session.commit()

    purge_resolved_alerts(session)
    session.commit()

    assert session.query(PrinterAlert).count() == 1
    session.close()


def test_recently_resolved_alerts_are_kept(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert
    from printaudit.monitoring.retention import purge_resolved_alerts

    session = SessionLocal()
    device = _make_device(session)
    session.add(
        PrinterAlert(
            printer_device_id=device.id, source="direct_snmp", alert_type="paper_jam", severity="critical",
            opened_at=datetime.now(timezone.utc) - timedelta(days=10),
            resolved_at=datetime.now(timezone.utc) - timedelta(days=5),
            external_id="paper_jam",
        )
    )
    session.commit()

    purge_resolved_alerts(session)
    session.commit()
    assert session.query(PrinterAlert).count() == 1
    session.close()


def test_old_resolved_alerts_are_purged(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert
    from printaudit.monitoring.retention import RESOLVED_ALERT_RETENTION_DAYS, purge_resolved_alerts

    session = SessionLocal()
    device = _make_device(session)
    session.add(
        PrinterAlert(
            printer_device_id=device.id, source="direct_snmp", alert_type="paper_jam", severity="critical",
            opened_at=datetime.now(timezone.utc) - timedelta(days=RESOLVED_ALERT_RETENTION_DAYS + 10),
            resolved_at=datetime.now(timezone.utc) - timedelta(days=RESOLVED_ALERT_RETENTION_DAYS + 5),
            external_id="paper_jam",
        )
    )
    session.commit()

    purge_resolved_alerts(session)
    session.commit()
    assert session.query(PrinterAlert).count() == 0
    session.close()


def test_run_retention_preserves_trend_before_purging_raw_data(app_env):
    """Ключевая гарантия: агрегация ВСЕГДА выполняется до удаления --
    тренд расходника не должен теряться безвозвратно."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterSupplyDailyAgg, PrinterSupplySample
    from printaudit.monitoring.retention import RAW_RETENTION_DAYS, run_retention

    session = SessionLocal()
    device = _make_device(session)
    _add_supply_sample(session, device, RAW_RETENTION_DAYS + 3, 42)
    session.commit()

    result = run_retention(session)

    assert result["aggregated_supply_days"] == 1
    assert result["supply"] == 1
    assert session.query(PrinterSupplySample).count() == 0  # сырой сэмпл удалён
    agg = session.query(PrinterSupplyDailyAgg).one()
    assert agg.avg_level_percent == 42  # но тренд остался
    session.close()
