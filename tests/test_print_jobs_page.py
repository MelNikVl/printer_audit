"""Тесты журнала заданий печати (/print-jobs), расширенного /api/print-jobs
и /export/csv: построчный вывод (не агрегат), все серверные фильтры,
серверная пагинация, переход из /by-user и /by-printer с предзаполненным
фильтром, цвет/Ч-б/не определено, отсутствие утечки document_name при
privacy-политике "masked"/"none"."""
from datetime import datetime, timedelta, timezone

from tests.conftest import login_as


def _make_job(session, **kwargs):
    from printaudit.models import PrintJob

    defaults = dict(
        site_code="TEST",
        record_id=kwargs.pop("record_id"),
        time_created=datetime.now(timezone.utc),
        user_name="DOMAIN\\ivanov",
        printer_name="HP-3F-BW",
        total_pages=10,
        is_color=None,
        color_source="unknown",
        cost=80.0,
        price_per_page=8.0,
    )
    defaults.update(kwargs)
    job = PrintJob(**defaults)
    session.add(job)
    return job


def test_print_jobs_page_lists_rows_not_aggregated(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, document_name="report.docx")
    _make_job(session, record_id=2, document_name="invoice.pdf", is_color=True, color_source="queue")
    session.commit()
    session.close()

    resp = http_client.get("/print-jobs")
    assert resp.status_code == 200
    assert "report.docx" in resp.text
    assert "invoice.pdf" in resp.text
    assert "Найдено заданий: 2" in resp.text


def test_print_jobs_page_shows_color_bw_and_unknown_labels(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, is_color=True, color_source="queue")
    _make_job(session, record_id=2, is_color=False, color_source="queue")
    _make_job(session, record_id=3, is_color=None, color_source="unknown")
    session.commit()
    session.close()

    resp = http_client.get("/print-jobs")
    assert resp.status_code == 200
    assert "Цветная" in resp.text
    assert "Ч/б" in resp.text
    assert "не определено" in resp.text


def test_print_jobs_page_filter_by_color(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, document_name="color-doc.pdf", is_color=True, color_source="queue")
    _make_job(session, record_id=2, document_name="bw-doc.pdf", is_color=False, color_source="queue")
    session.commit()
    session.close()

    resp = http_client.get("/print-jobs", params={"color": "color"})
    assert "color-doc.pdf" in resp.text
    assert "bw-doc.pdf" not in resp.text


def test_print_jobs_page_filter_by_document_search(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, document_name="salary_report.xlsx")
    _make_job(session, record_id=2, document_name="unrelated.pdf")
    session.commit()
    session.close()

    resp = http_client.get("/print-jobs", params={"q": "salary"})
    assert "salary_report.xlsx" in resp.text
    assert "unrelated.pdf" not in resp.text


def test_print_jobs_page_filter_by_user_and_printer(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, user_name="DOMAIN\\ivanov", printer_name="HP-A", document_name="a.pdf")
    _make_job(session, record_id=2, user_name="DOMAIN\\petrov", printer_name="HP-B", document_name="b.pdf")
    session.commit()
    session.close()

    resp = http_client.get("/print-jobs", params={"user_name": "DOMAIN\\ivanov"})
    assert "a.pdf" in resp.text
    assert "b.pdf" not in resp.text

    resp = http_client.get("/print-jobs", params={"printer_name": "HP-B"})
    assert "b.pdf" in resp.text
    assert "a.pdf" not in resp.text


def test_print_jobs_page_filter_by_department(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import Department

    login_as(http_client, role="viewer")
    session = SessionLocal()
    dept = Department(name="Бухгалтерия")
    session.add(dept)
    session.flush()
    _make_job(session, record_id=1, department_id=dept.id, document_name="dept-doc.pdf")
    _make_job(session, record_id=2, department_id=None, document_name="nodept-doc.pdf")
    session.commit()
    dept_id = dept.id
    session.close()

    resp = http_client.get("/print-jobs", params={"department_id": dept_id})
    assert "dept-doc.pdf" in resp.text
    assert "nodept-doc.pdf" not in resp.text


def test_print_jobs_page_filter_by_date_range(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    _make_job(session, record_id=1, time_created=old, document_name="old.pdf")
    _make_job(session, record_id=2, document_name="recent.pdf")
    session.commit()
    session.close()

    today = datetime.now(timezone.utc).date()
    resp = http_client.get(
        "/print-jobs",
        params={"date_from": today.isoformat(), "date_to": today.isoformat()},
    )
    assert "recent.pdf" in resp.text
    assert "old.pdf" not in resp.text


def test_print_jobs_page_filter_by_site_and_print_server(http_client):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_print_server, get_or_create_site

    login_as(http_client, role="viewer")
    session = SessionLocal()
    site_a = get_or_create_site(session, "SITE-A", name="Площадка А")
    site_b = get_or_create_site(session, "SITE-B", name="Площадка Б")
    server_a = get_or_create_print_server(session, site_a, "PRN-A1")
    server_b = get_or_create_print_server(session, site_b, "PRN-B1")
    session.flush()
    _make_job(session, record_id=1, site_id=site_a.id, print_server_id=server_a.id, document_name="from-a.pdf")
    _make_job(session, record_id=2, site_id=site_b.id, print_server_id=server_b.id, document_name="from-b.pdf")
    session.commit()
    site_a_id = site_a.id
    server_a_id = server_a.id
    session.close()

    resp = http_client.get("/print-jobs", params={"site_id": site_a_id})
    assert "from-a.pdf" in resp.text
    assert "from-b.pdf" not in resp.text

    resp = http_client.get("/print-jobs", params={"print_server_id": server_a_id})
    assert "from-a.pdf" in resp.text
    assert "from-b.pdf" not in resp.text


def test_print_jobs_page_pagination_does_not_load_everything(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    for i in range(1, 6):
        _make_job(session, record_id=i, document_name=f"doc{i}.pdf")
    session.commit()
    session.close()

    resp = http_client.get("/print-jobs", params={"page_size": 2, "page": 1})
    assert resp.status_code == 200
    assert "Страница 1 из 3" in resp.text
    shown = sum(1 for i in range(1, 6) if f"doc{i}.pdf" in resp.text)
    assert shown == 2

    resp2 = http_client.get("/print-jobs", params={"page_size": 2, "page": 2})
    shown2 = sum(1 for i in range(1, 6) if f"doc{i}.pdf" in resp2.text)
    assert shown2 == 2
    assert resp.text != resp2.text


def test_by_user_report_links_to_print_jobs_with_prefilled_filter(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, user_name="DOMAIN\\ivanov")
    session.commit()
    session.close()

    resp = http_client.get("/by-user")
    assert resp.status_code == 200
    assert "/print-jobs?user_name=DOMAIN%5Civanov" in resp.text


def test_by_printer_report_links_to_print_jobs_with_prefilled_filter(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, printer_name="HP-3F-BW")
    session.commit()
    session.close()

    resp = http_client.get("/by-printer")
    assert resp.status_code == 200
    assert "/print-jobs?printer_name=HP-3F-BW" in resp.text


def test_api_print_jobs_includes_new_fields_and_total_count_header(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(
        session, record_id=1, document_name="a.pdf", copies=2, pages_per_copy=5,
        source_computer="PC-01", is_color=True, color_source="queue",
    )
    session.commit()
    session.close()

    resp = http_client.get("/api/print-jobs")
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "1"
    data = resp.json()
    assert len(data) == 1
    row = data[0]
    for key in (
        "site", "print_server", "department", "source_computer", "copies",
        "pages_per_copy", "is_color", "color_source", "currency",
    ):
        assert key in row
    assert row["copies"] == 2
    assert row["pages_per_copy"] == 5
    assert row["source_computer"] == "PC-01"


def test_api_print_jobs_respects_color_filter(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, is_color=None, color_source="unknown")
    _make_job(session, record_id=2, is_color=True, color_source="queue")
    session.commit()
    session.close()

    resp = http_client.get("/api/print-jobs", params={"color": "unknown"})
    data = resp.json()
    assert len(data) == 1
    assert data[0]["is_color"] is None
    assert data[0]["color_source"] == "unknown"


def test_export_csv_includes_new_columns(http_client):
    from printaudit.database import SessionLocal

    login_as(http_client, role="viewer")
    session = SessionLocal()
    _make_job(session, record_id=1, document_name="a.pdf", copies=3, pages_per_copy=2, source_computer="PC-07")
    session.commit()
    session.close()

    resp = http_client.get("/export/csv")
    assert resp.status_code == 200
    body = resp.text
    header = body.splitlines()[0]
    for col in ("copies", "pages_per_copy", "source_computer", "color_source", "currency"):
        assert col in header
    assert "PC-07" in body


def test_document_name_masked_policy_not_bypassed_in_journal_api_or_csv(http_client, monkeypatch):
    """Журнал/API/CSV должны показывать то, что реально хранится в БД —
    если privacy-политика замаскировала document_name на момент вставки
    (см. printaudit.privacy), новый функционал не должен давать способ
    обойти это и увидеть настоящее имя (оно и не хранится вообще)."""
    from printaudit.database import SessionLocal
    from printaudit.privacy import apply_document_name_policy

    login_as(http_client, role="viewer")
    session = SessionLocal()
    stored_name = apply_document_name_policy("Зарплата_Иванов.xlsx", "masked")
    _make_job(session, record_id=1, document_name=stored_name)
    session.commit()
    session.close()

    assert stored_name == "•••.xlsx"

    resp = http_client.get("/print-jobs")
    assert "Зарплата_Иванов" not in resp.text
    assert "•••.xlsx" in resp.text

    resp = http_client.get("/api/print-jobs")
    body = resp.text
    assert "Зарплата_Иванов" not in body

    resp = http_client.get("/export/csv")
    assert "Зарплата_Иванов" not in resp.text
    assert "•••.xlsx" in resp.text


def test_print_jobs_page_requires_login(http_client):
    resp = http_client.get("/print-jobs", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
