"""UI/dashboard and admin flows added for Print Management v. 02."""
from tests.conftest import login_as


def _csrf(http_client, url):
    http_client.get(url)
    return http_client.cookies.get("pa_csrf")


def test_root_is_user_dashboard_with_sidebar_and_brand(http_client):
    login_as(http_client, role="admin")
    page = http_client.get("/")
    assert page.status_code == 200
    assert "Дашборд по пользователям" in page.text
    assert "Print Management" in page.text
    assert "v. 02" in page.text
    assert "Заданий печати" in page.text
    assert 'class="app-sidebar"' in page.text


def test_site_display_name_can_be_changed_without_changing_code(http_client):
    login_as(http_client, role="admin")
    from printaudit.database import SessionLocal
    from printaudit.models import AuditLog
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "RENAME-SITE", name="Старое название")
    session.commit()
    site_id = site.id
    session.close()

    csrf = _csrf(http_client, "/admin/sites")
    response = http_client.post(
        f"/admin/sites/{site_id}/update",
        data={"csrf_token": csrf, "name": "Новое название", "description": "Новый офис"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    session = SessionLocal()
    site = session.get(__import__("printaudit.models", fromlist=["Site"]).Site, site_id)
    assert site.site_code == "RENAME-SITE"
    assert site.name == "Новое название"
    assert site.description == "Новый офис"
    assert session.query(AuditLog).filter_by(action="site.update", object_id=str(site_id)).count() == 1
    session.close()


def test_admin_can_create_device_and_explicitly_link_queue(http_client):
    login_as(http_client, role="admin")
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterDevice, PrinterDeviceQueueLink, PrinterQueue
    from printaudit.sites import get_or_create_print_server, get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "DEVICE-SITE", name="Device Site")
    server = get_or_create_print_server(session, site, "PRINT-DEVICE")
    session.flush()
    queue = PrinterQueue(
        printer_name="Office-BW", print_server_id=server.id,
        display_name="Office BW", server_name=server.server_name,
    )
    session.add(queue)
    session.commit()
    site_id, queue_id = site.id, queue.id
    session.close()

    csrf = _csrf(http_client, "/admin/printers")
    response = http_client.post(
        "/admin/printer-devices/create",
        data={
            "csrf_token": csrf,
            "site_id": site_id,
            "display_name": "HP Office",
            "ip_address": "10.1.2.3",
            "monitoring_source": "disabled",
            "queue_id": queue_id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "msg=" in response.headers["location"]

    session = SessionLocal()
    device = session.query(PrinterDevice).filter_by(display_name="HP Office").one()
    assert device.site_id == site_id
    assert device.ip_address == "10.1.2.3"
    assert device.monitoring_source == "disabled"
    link = session.query(PrinterDeviceQueueLink).filter_by(
        printer_device_id=device.id, printer_queue_id=queue_id, is_active=True
    ).one()
    assert link is not None
    session.close()


def test_direct_snmp_device_requires_profile(http_client):
    login_as(http_client, role="admin")
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SNMP-VALIDATE", name="SNMP Validate")
    session.commit()
    site_id = site.id
    session.close()

    csrf = _csrf(http_client, "/admin/printers")
    response = http_client.post(
        "/admin/printer-devices/create",
        data={
            "csrf_token": csrf, "site_id": site_id, "display_name": "No profile",
            "ip_address": "10.2.3.4", "monitoring_source": "direct_snmp",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
