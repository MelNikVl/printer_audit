"""printaudit.forecasting.series.build_daily_series — плотный ежедневный
ряд по каждому охвату (устройство/очередь/площадка/организация), нулевое
заполнение дней без заданий, разделение color/bw по PrintJob.is_color."""
from datetime import date, datetime, timedelta

from printaudit.forecasting import METRIC_BW_PAGES, METRIC_COLOR_PAGES, METRIC_COST, METRIC_JOB_COUNT, METRIC_TOTAL_PAGES, SCOPE_DEVICE, SCOPE_ORGANIZATION, SCOPE_QUEUE, SCOPE_SITE
from printaudit.forecasting.series import build_daily_series


def _make_job(session, site, day, printer_queue_id=None, total_pages=10, is_color=None, cost=80.0, record_id=None):
    from printaudit.models import PrintJob

    session.add(
        PrintJob(
            site_code=site.site_code, site_id=site.id, record_id=record_id,
            time_created=datetime(day.year, day.month, day.day, 10, 0),
            user_name="DOMAIN\\ivanov", printer_name="HP-SHARED", printer_queue_id=printer_queue_id,
            total_pages=total_pages, is_color=is_color, cost=cost, price_per_page=8.0,
        )
    )


def test_organization_scope_sums_across_sites(app_env):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site_a = get_or_create_site(session, "SITE-A", name="A")
    site_b = get_or_create_site(session, "SITE-B", name="B")
    session.flush()
    today = date(2026, 9, 3)
    _make_job(session, site_a, today - timedelta(days=1), total_pages=10, record_id=1)
    _make_job(session, site_b, today - timedelta(days=1), total_pages=5, record_id=2)
    session.commit()

    series = build_daily_series(session, SCOPE_ORGANIZATION, None, METRIC_TOTAL_PAGES, end_date=today, num_days=7)
    session.close()
    assert series[-1] == 15.0
    assert len(series) == 7


def test_site_scope_isolates_from_other_sites(app_env):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site_a = get_or_create_site(session, "SITE-A", name="A")
    site_b = get_or_create_site(session, "SITE-B", name="B")
    session.flush()
    today = date(2026, 9, 3)
    _make_job(session, site_a, today - timedelta(days=1), total_pages=10, record_id=1)
    _make_job(session, site_b, today - timedelta(days=1), total_pages=999, record_id=2)
    session.commit()
    site_a_id = site_a.id

    series = build_daily_series(session, SCOPE_SITE, site_a_id, METRIC_TOTAL_PAGES, end_date=today, num_days=7)
    session.close()
    assert series[-1] == 10.0


def test_missing_days_are_zero_filled(app_env):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 10)
    _make_job(session, site, today - timedelta(days=5), total_pages=42, record_id=1)
    session.commit()
    site_id = site.id

    series = build_daily_series(session, SCOPE_SITE, site_id, METRIC_TOTAL_PAGES, end_date=today, num_days=7)
    session.close()
    assert series == [0.0, 0.0, 42.0, 0.0, 0.0, 0.0, 0.0]


def test_job_count_metric_counts_jobs_not_pages(app_env):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _make_job(session, site, today - timedelta(days=1), total_pages=100, record_id=1)
    _make_job(session, site, today - timedelta(days=1), total_pages=1, record_id=2)
    session.commit()
    site_id = site.id

    series = build_daily_series(session, SCOPE_SITE, site_id, METRIC_JOB_COUNT, end_date=today, num_days=7)
    session.close()
    assert series[-1] == 2.0


def test_color_and_bw_pages_split_by_is_color_unknown_excluded_from_both(app_env):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _make_job(session, site, today - timedelta(days=1), total_pages=10, is_color=True, record_id=1)
    _make_job(session, site, today - timedelta(days=1), total_pages=5, is_color=False, record_id=2)
    _make_job(session, site, today - timedelta(days=1), total_pages=7, is_color=None, record_id=3)  # неизвестно
    session.commit()
    site_id = site.id

    color_series = build_daily_series(session, SCOPE_SITE, site_id, METRIC_COLOR_PAGES, end_date=today, num_days=7)
    bw_series = build_daily_series(session, SCOPE_SITE, site_id, METRIC_BW_PAGES, end_date=today, num_days=7)
    total_series = build_daily_series(session, SCOPE_SITE, site_id, METRIC_TOTAL_PAGES, end_date=today, num_days=7)
    session.close()
    assert color_series[-1] == 10.0
    assert bw_series[-1] == 5.0
    assert total_series[-1] == 22.0  # is_color=None всё равно учитывается в total_pages


def test_device_scope_follows_linked_queue(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser, PrinterQueue
    from printaudit.monitoring.devices import create_device, link_queue
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
    session.add(actor)
    session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="HP")
    queue = PrinterQueue(printer_name="HP-Q")
    session.add(queue)
    session.flush()
    link_queue(session, actor=actor, device=device, queue=queue)
    session.commit()
    device_id, queue_id = device.id, queue.id

    today = date(2026, 9, 3)
    _make_job(session, site, today - timedelta(days=1), printer_queue_id=queue_id, total_pages=33, record_id=1)
    # Задание с ДРУГОЙ, не связанной очередью не должно попадать в ряд устройства.
    other_queue = PrinterQueue(printer_name="OTHER-Q")
    session.add(other_queue)
    session.flush()
    _make_job(session, site, today - timedelta(days=1), printer_queue_id=other_queue.id, total_pages=999, record_id=2)
    session.commit()

    series = build_daily_series(session, SCOPE_DEVICE, device_id, METRIC_TOTAL_PAGES, end_date=today, num_days=7)
    session.close()
    assert series[-1] == 33.0


def test_device_scope_with_no_linked_queue_returns_all_zero(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
    session.add(actor)
    session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="Unlinked")
    session.commit()
    device_id = device.id

    series = build_daily_series(session, SCOPE_DEVICE, device_id, METRIC_TOTAL_PAGES, end_date=date(2026, 9, 3), num_days=7)
    session.close()
    assert series == [0.0] * 7


def test_end_date_itself_is_excluded_as_incomplete_day(app_env):
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _make_job(session, site, today, total_pages=999, record_id=1)  # сегодняшний, ещё не завершённый день
    session.commit()
    site_id = site.id

    series = build_daily_series(session, SCOPE_SITE, site_id, METRIC_TOTAL_PAGES, end_date=today, num_days=7)
    session.close()
    assert sum(series) == 0.0
