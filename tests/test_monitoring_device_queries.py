"""printaudit.monitoring.device_queries — список устройств для /printers
(фильтры по статусу/ошибкам/расходникам) и карточка устройства
/printers/{id} (последние сэмплы, связанные очереди, алерты, прогнозы)."""
from datetime import datetime, timedelta

from printaudit.monitoring import DEVICE_STATUS_ERROR, DEVICE_STATUS_ONLINE
from printaudit.monitoring.device_queries import dashboard_summary, get_device_detail, list_devices


def _actor(session):
    from printaudit.models import AppUser

    actor = session.query(AppUser).filter_by(login_normalized="domain\\actor").first()
    if actor is None:
        actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
        session.add(actor)
        session.flush()
    return actor


def _make_device(session, site_code="SITE-A", **overrides):
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_site

    site = get_or_create_site(session, site_code, name=site_code)
    actor = _actor(session)
    device = create_device(session, actor=actor, site_id=site.id, display_name=overrides.pop("display_name", "HP LaserJet"), **overrides)
    session.commit()
    return device


def _add_health(session, device, is_reachable=True, device_status="online", collected_at=None, source="direct_snmp"):
    from printaudit.models import PrinterHealthSample

    session.add(
        PrinterHealthSample(
            printer_device_id=device.id, collected_at=collected_at or datetime.utcnow(), source=source,
            is_reachable=is_reachable, device_status=device_status,
        )
    )
    session.commit()


def _add_supply(session, device, supply_type="toner_black", level_percent=50.0, level_status="ok", collected_at=None):
    from printaudit.models import PrinterSupplySample

    session.add(
        PrinterSupplySample(
            printer_device_id=device.id, collected_at=collected_at or datetime.utcnow(), source="direct_snmp",
            supply_type=supply_type, level_percent=level_percent, level_status=level_status,
        )
    )
    session.commit()


def test_list_devices_filters_by_status(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    online_device = _make_device(session, display_name="Online-Printer")
    error_device = _make_device(session, display_name="Error-Printer")
    _add_health(session, online_device, is_reachable=True, device_status="online")
    _add_health(session, error_device, is_reachable=True, device_status="error")

    online_rows = list_devices(session, status=DEVICE_STATUS_ONLINE)
    error_rows = list_devices(session, status=DEVICE_STATUS_ERROR)
    session.close()

    assert {r.device.display_name for r in online_rows} == {"Online-Printer"}
    assert {r.device.display_name for r in error_rows} == {"Error-Printer"}


def test_list_devices_no_data_only_excludes_devices_with_samples(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    with_data = _make_device(session, display_name="Has-Data")
    without_data = _make_device(session, display_name="No-Data")
    _add_health(session, with_data)

    rows = list_devices(session, no_data_only=True)
    session.close()
    assert {r.device.display_name for r in rows} == {"No-Data"}


def test_list_devices_low_supply_filter(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    low = _make_device(session, display_name="Low-Toner")
    ok = _make_device(session, display_name="OK-Toner")
    _add_supply(session, low, level_percent=10.0, level_status="low")
    _add_supply(session, ok, level_percent=80.0, level_status="ok")

    rows = list_devices(session, low_supply_only=True)
    session.close()
    assert {r.device.display_name for r in rows} == {"Low-Toner"}


def test_list_devices_active_errors_filter(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert

    session = SessionLocal()
    with_alert = _make_device(session, display_name="With-Alert")
    without_alert = _make_device(session, display_name="Without-Alert")
    session.add(
        PrinterAlert(
            printer_device_id=with_alert.id, source="direct_snmp", alert_type="hardware_error",
            severity="warning", external_id="hardware_error", opened_at=datetime.utcnow(),
        )
    )
    session.commit()

    rows = list_devices(session, has_active_errors=True)
    session.close()
    assert {r.device.display_name for r in rows} == {"With-Alert"}


def test_list_devices_site_filter_isolates_sites(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    dev_a = _make_device(session, site_code="SITE-A", display_name="A-Printer")
    dev_b = _make_device(session, site_code="SITE-B", display_name="B-Printer")
    site_a_id = dev_a.site_id

    rows = list_devices(session, site_id=site_a_id)
    session.close()
    assert {r.device.display_name for r in rows} == {"A-Printer"}


def test_dashboard_summary_counts_by_status_and_problems(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    online = _make_device(session, display_name="Online")
    error = _make_device(session, display_name="Error")
    _add_health(session, online, device_status="online")
    _add_health(session, error, device_status="error")

    summary = dashboard_summary(session)
    session.close()
    assert summary.total_devices == 2
    assert summary.online == 1
    assert summary.error == 1
    assert summary.sites_with_problems == 1


def test_get_device_detail_returns_none_for_unknown_id(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    detail = get_device_detail(session, 999999)
    session.close()
    assert detail is None


def test_get_device_detail_includes_health_supplies_alerts_queues(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterAlert, PrinterQueue
    from printaudit.monitoring.devices import link_queue

    session = SessionLocal()
    device = _make_device(session, display_name="Full-Detail")
    _add_health(session, device, device_status="warning")
    _add_supply(session, device, supply_type="toner_black", level_percent=15.0, level_status="low")
    session.add(
        PrinterAlert(
            printer_device_id=device.id, source="direct_snmp", alert_type="paper_out",
            severity="warning", external_id="paper_out", opened_at=datetime.utcnow(),
        )
    )
    queue = PrinterQueue(printer_name="Q1")
    session.add(queue)
    session.flush()
    link_queue(session, actor=_actor(session), device=device, queue=queue)
    session.commit()
    device_id = device.id

    detail = get_device_detail(session, device_id)
    session.close()

    assert detail.status == "warning"
    assert detail.latest_health is not None
    assert len(detail.supplies) == 1
    assert len(detail.alerts) == 1
    assert len(detail.linked_queues) == 1


def test_get_device_detail_jobs_period_sums_linked_queues(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue, PrintJob
    from printaudit.monitoring.devices import link_queue
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    device = _make_device(session, display_name="Job-Count-Device")
    site = get_or_create_site(session, "SITE-A", name="SITE-A")
    queue = PrinterQueue(printer_name="Q1")
    session.add(queue)
    session.flush()
    link_queue(session, actor=_actor(session), device=device, queue=queue)
    session.add(
        PrintJob(
            site_code=site.site_code, site_id=site.id, record_id=1, time_created=datetime.utcnow(),
            user_name="DOMAIN\\ivanov", printer_name="Q1", printer_queue_id=queue.id, total_pages=7, cost=10.0,
        )
    )
    session.commit()
    device_id = device.id

    detail = get_device_detail(session, device_id, period_days=30)
    session.close()
    assert detail.jobs_period == 1
    assert detail.pages_period == 7
