"""printaudit.forecasting.pipeline — сохранение ForecastRun: идемпотентный
upsert (в т.ч. КРИТИЧНО для scope_type=organization, где scope_id=NULL и
поэтому UniqueConstraint на уровне БД не защищает от дублей — см. docstring
модуля), недостаточная история помечена явно, тонер/риск простоя."""
from datetime import date, datetime, timedelta

from printaudit.forecasting import HORIZON_DAYS, METRIC_TOTAL_PAGES, SCOPE_ORGANIZATION, SCOPE_SITE


def _make_job(session, site, day, total_pages=10, record_id=None):
    from printaudit.models import PrintJob

    session.add(
        PrintJob(
            site_code=site.site_code, site_id=site.id, record_id=record_id,
            time_created=datetime(day.year, day.month, day.day, 10, 0),
            user_name="DOMAIN\\ivanov", printer_name="HP-SHARED",
            total_pages=total_pages, cost=80.0, price_per_page=8.0,
        )
    )


def _seed_days(session, site, end_date, n_days, pages_fn, start_record_id=1):
    rid = start_record_id
    for i in range(n_days):
        day = end_date - timedelta(days=n_days - i)
        pages = pages_fn(i)
        if pages > 0:
            _make_job(session, site, day, total_pages=pages, record_id=rid)
            rid += 1
    return rid


def test_compute_load_forecasts_marks_insufficient_history_when_too_little_data(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_load_forecasts
    from printaudit.models import ForecastRun
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _seed_days(session, site, today, 5, lambda i: 10)  # мало истории
    session.commit()
    site_id = site.id

    compute_load_forecasts(session, SCOPE_SITE, site_id, now=today)
    session.commit()

    rows = session.query(ForecastRun).filter_by(scope_type=SCOPE_SITE, scope_id=site_id, metric=METRIC_TOTAL_PAGES).all()
    session.close()
    assert len(rows) == len(HORIZON_DAYS)
    assert all(r.insufficient_history for r in rows)


def test_compute_load_forecasts_produces_forecast_with_enough_history(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_load_forecasts
    from printaudit.models import ForecastRun
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _seed_days(session, site, today, 200, lambda i: 10 + (i % 7))
    session.commit()
    site_id = site.id

    compute_load_forecasts(session, SCOPE_SITE, site_id, now=today)
    session.commit()

    row = (
        session.query(ForecastRun)
        .filter_by(scope_type=SCOPE_SITE, scope_id=site_id, metric=METRIC_TOTAL_PAGES, horizon_days=7)
        .one()
    )
    session.close()
    assert row.insufficient_history is False
    assert row.model_name is not None
    assert row.forecast_json is not None


def test_compute_load_forecasts_is_idempotent_no_duplicate_rows(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_load_forecasts
    from printaudit.models import ForecastRun
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _seed_days(session, site, today, 30, lambda i: 5)
    session.commit()
    site_id = site.id

    compute_load_forecasts(session, SCOPE_SITE, site_id, now=today)
    session.commit()
    compute_load_forecasts(session, SCOPE_SITE, site_id, now=today)
    session.commit()

    count = session.query(ForecastRun).filter_by(scope_type=SCOPE_SITE, scope_id=site_id, metric=METRIC_TOTAL_PAGES).count()
    session.close()
    assert count == len(HORIZON_DAYS)  # не задвоилось


def test_organization_scope_upsert_does_not_duplicate_despite_null_scope_id(app_env):
    """Регрессионный тест на КЛЮЧЕВОЙ риск: scope_id=NULL для organization
    означает, что UniqueConstraint(scope_type, scope_id, metric,
    horizon_days) на уровне БД НЕ защищает от дублей (NULL != NULL в SQL)
    -- защита должна быть явной (запрос-затем-запись в pipeline.py)."""
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_load_forecasts
    from printaudit.models import ForecastRun
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    today = date(2026, 9, 3)
    _seed_days(session, site, today, 10, lambda i: 5)
    session.commit()

    compute_load_forecasts(session, SCOPE_ORGANIZATION, None, now=today)
    session.commit()
    compute_load_forecasts(session, SCOPE_ORGANIZATION, None, now=today)
    session.commit()
    compute_load_forecasts(session, SCOPE_ORGANIZATION, None, now=today)
    session.commit()

    count = session.query(ForecastRun).filter_by(scope_type=SCOPE_ORGANIZATION, metric=METRIC_TOTAL_PAGES).count()
    session.close()
    assert count == len(HORIZON_DAYS)


def _make_device(session, site_code="SITE-A"):
    from printaudit.models import AppUser
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_site

    site = get_or_create_site(session, site_code, name=site_code)
    actor = session.query(AppUser).filter_by(login_normalized="domain\\actor").first()
    if actor is None:
        actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
        session.add(actor)
        session.flush()
    device = create_device(session, actor=actor, site_id=site.id, display_name="HP LaserJet")
    session.commit()
    return device


def test_toner_exhaustion_forecast_row_per_supply_type(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting import METRIC_TONER_EXHAUSTION
    from printaudit.forecasting.pipeline import compute_toner_exhaustion
    from printaudit.models import ForecastRun, PrinterSupplyDailyAgg

    session = SessionLocal()
    device = _make_device(session)
    today = date(2026, 9, 10)
    for i, level in enumerate([80, 70, 60, 50, 40, 30]):
        session.add(
            PrinterSupplyDailyAgg(
                printer_device_id=device.id, supply_type="toner_black",
                day=today - timedelta(days=6 - i), avg_level_percent=level, sample_count=10,
            )
        )
    session.commit()
    device_id = device.id

    compute_toner_exhaustion(session, device, now=today)
    session.commit()

    row = (
        session.query(ForecastRun)
        .filter_by(scope_type="device", scope_id=device_id, metric=f"{METRIC_TONER_EXHAUSTION}:toner_black")
        .one()
    )
    session.close()
    assert row.insufficient_history is False
    assert "exhaustion_date" in row.forecast_json


def test_toner_exhaustion_insufficient_when_no_supply_data(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_toner_exhaustion
    from printaudit.models import ForecastRun

    session = SessionLocal()
    device = _make_device(session)
    device_id = device.id

    rows = compute_toner_exhaustion(session, device, now=date(2026, 9, 10))
    session.commit()
    session.close()
    assert rows == []  # без данных о расходниках -- строк вообще нет, не фиктивные


def test_downtime_risk_high_with_active_critical_alert(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_downtime_risk
    from printaudit.models import PrinterAlert, PrinterHealthSample

    session = SessionLocal()
    device = _make_device(session)
    now = datetime(2026, 9, 10, 12, 0)
    session.add(
        PrinterHealthSample(
            printer_device_id=device.id, collected_at=now - timedelta(hours=1), source="direct_snmp",
            is_reachable=True, device_status="online",
        )
    )
    session.add(
        PrinterAlert(
            printer_device_id=device.id, source="direct_snmp", alert_type="hardware_error",
            severity="critical", external_id="hardware_error", opened_at=now - timedelta(hours=2),
        )
    )
    session.commit()

    row = compute_downtime_risk(session, device, now=now)
    session.commit()
    insufficient, forecast_json = row.insufficient_history, row.forecast_json
    session.close()
    assert insufficient is False
    import json

    payload = json.loads(forecast_json)
    assert payload["level"] == "high"


def test_downtime_risk_insufficient_without_health_samples(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_downtime_risk

    session = SessionLocal()
    device = _make_device(session)
    row = compute_downtime_risk(session, device, now=datetime(2026, 9, 10, 12, 0))
    session.commit()
    insufficient = row.insufficient_history
    session.close()
    assert insufficient is True


def test_compute_all_forecasts_end_to_end_smoke(app_env):
    from printaudit.database import SessionLocal
    from printaudit.forecasting.pipeline import compute_all_forecasts
    from printaudit.models import ForecastRun
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="A")
    session.flush()
    _seed_days(session, site, date(2026, 9, 3), 20, lambda i: 3)
    device = _make_device(session, site_code="SITE-A")
    session.commit()

    counts = compute_all_forecasts(session, now=datetime(2026, 9, 3, 9, 0))
    assert counts["devices"] >= 1
    assert counts["sites"] >= 1
    assert counts["organization"] == 1
    assert session.query(ForecastRun).count() > 0
    session.close()
