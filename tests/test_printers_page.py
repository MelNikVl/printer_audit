"""Тесты /printers (список устройств) и /printers/{id} (карточка) — UI
поверх printaudit.monitoring.device_queries, а также виджеты мониторинга на
/admin (Обзор)."""
from datetime import datetime

from tests.conftest import login_as


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


def test_printers_page_requires_login(http_client):
    resp = http_client.get("/printers", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_printers_page_lists_devices(http_client):
    login_as(http_client, role="viewer")
    from printaudit.database import SessionLocal

    session = SessionLocal()
    _make_device(session, display_name="Front-Desk-HP")
    session.close()

    resp = http_client.get("/printers")
    assert resp.status_code == 200
    assert "Front-Desk-HP" in resp.text


def test_printers_page_status_filter(http_client):
    login_as(http_client, role="viewer")
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterHealthSample

    session = SessionLocal()
    online = _make_device(session, display_name="Online-Device")
    error = _make_device(session, display_name="Error-Device")
    session.add(PrinterHealthSample(printer_device_id=online.id, collected_at=datetime.utcnow(), source="direct_snmp", is_reachable=True, device_status="online"))
    session.add(PrinterHealthSample(printer_device_id=error.id, collected_at=datetime.utcnow(), source="direct_snmp", is_reachable=True, device_status="error"))
    session.commit()
    session.close()

    resp = http_client.get("/printers?status=error")
    assert resp.status_code == 200
    assert "Error-Device" in resp.text
    assert "Online-Device" not in resp.text


def test_printer_detail_page_404_for_unknown_device(http_client):
    login_as(http_client, role="viewer")
    resp = http_client.get("/printers/999999")
    assert resp.status_code == 404


def test_printer_detail_page_shows_health_and_supplies(http_client):
    login_as(http_client, role="viewer")
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterHealthSample, PrinterSupplySample

    session = SessionLocal()
    device = _make_device(session, display_name="Detail-Device")
    session.add(PrinterHealthSample(printer_device_id=device.id, collected_at=datetime.utcnow(), source="direct_snmp", is_reachable=True, device_status="warning"))
    session.add(PrinterSupplySample(printer_device_id=device.id, collected_at=datetime.utcnow(), source="direct_snmp", supply_type="toner_black", level_percent=12.0, level_status="low"))
    session.commit()
    device_id = device.id
    session.close()

    resp = http_client.get(f"/printers/{device_id}")
    assert resp.status_code == 200
    assert "Detail-Device" in resp.text
    assert "toner_black" in resp.text


def test_printer_detail_page_shows_insufficient_data_for_forecasts_without_history(http_client):
    login_as(http_client, role="viewer")
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_load_forecasts
    from printaudit.forecasting import SCOPE_DEVICE

    session = SessionLocal()
    device = _make_device(session, display_name="New-Device")
    device_id = device.id
    compute_load_forecasts(session, SCOPE_DEVICE, device_id, now=datetime(2026, 9, 3).date())
    session.commit()
    session.close()

    resp = http_client.get(f"/printers/{device_id}")
    assert resp.status_code == 200
    assert "Недостаточно данных" in resp.text


def test_admin_overview_shows_monitoring_summary(http_client):
    login_as(http_client, role="admin")
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterHealthSample

    session = SessionLocal()
    device = _make_device(session, display_name="Overview-Device")
    session.add(PrinterHealthSample(printer_device_id=device.id, collected_at=datetime.utcnow(), source="direct_snmp", is_reachable=True, device_status="online"))
    session.commit()
    session.close()

    resp = http_client.get("/admin")
    assert resp.status_code == 200
    assert "Мониторинг принтеров" in resp.text
