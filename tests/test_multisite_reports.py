"""Требование Части 6: данные разных площадок не должны смешиваться —
дашборд, отчёты по отделам/пользователям/принтерам должны корректно
фильтроваться по site_id и не путать совпадающие имена принтеров/логины
между площадками."""
from datetime import datetime, timezone

from tests.conftest import login_as


def _make_job(session, site, **kwargs):
    from printaudit.models import PrintJob

    defaults = dict(
        site_code=site.site_code, site_id=site.id, record_id=kwargs.pop("record_id"),
        time_created=datetime.now(timezone.utc), user_name="DOMAIN\\ivanov",
        printer_name="HP-SHARED", total_pages=10, cost=80.0, price_per_page=8.0,
    )
    defaults.update(kwargs)
    session.add(PrintJob(**defaults))


def test_dashboard_site_filter_isolates_data(http_client):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    login_as(http_client, role="viewer")
    session = SessionLocal()
    site_a = get_or_create_site(session, "SITE-A", name="Площадка А")
    site_b = get_or_create_site(session, "SITE-B", name="Площадка Б")
    session.flush()
    _make_job(session, site_a, record_id=1, total_pages=100, cost=800.0)
    _make_job(session, site_b, record_id=2, total_pages=5, cost=40.0)
    session.commit()
    site_a_id = site_a.id
    session.close()

    resp_all = http_client.get("/")
    assert "105" in resp_all.text or "100" in resp_all.text  # total pages across both sites present somewhere

    resp_a = http_client.get("/", params={"site_id": site_a_id})
    assert resp_a.status_code == 200
    assert "800.00" in resp_a.text
    assert "40.00" not in resp_a.text


def test_by_user_report_site_filter_does_not_mix_sites(http_client):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    login_as(http_client, role="viewer")
    session = SessionLocal()
    site_a = get_or_create_site(session, "SITE-A", name="Площадка А")
    site_b = get_or_create_site(session, "SITE-B", name="Площадка Б")
    session.flush()
    _make_job(session, site_a, record_id=1, user_name="DOMAIN\\ivanov", cost=100.0)
    _make_job(session, site_b, record_id=2, user_name="DOMAIN\\ivanov", cost=999.0)
    session.commit()
    site_a_id = site_a.id
    session.close()

    resp = http_client.get("/by-user", params={"site_id": site_a_id})
    assert "100.00" in resp.text
    assert "999.00" not in resp.text


def test_by_printer_report_same_printer_name_two_sites_not_merged_when_filtered(http_client):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    login_as(http_client, role="viewer")
    session = SessionLocal()
    site_a = get_or_create_site(session, "SITE-A", name="Площадка А")
    site_b = get_or_create_site(session, "SITE-B", name="Площадка Б")
    session.flush()
    _make_job(session, site_a, record_id=1, printer_name="HP-SHARED", cost=11.0)
    _make_job(session, site_b, record_id=2, printer_name="HP-SHARED", cost=22.0)
    session.commit()
    site_a_id = site_a.id
    session.close()

    resp_unfiltered = http_client.get("/by-printer")
    assert "33.00" in resp_unfiltered.text  # aggregated across both sites by printer_name

    resp_a = http_client.get("/by-printer", params={"site_id": site_a_id})
    assert "11.00" in resp_a.text
    assert "33.00" not in resp_a.text
    assert "22.00" not in resp_a.text
