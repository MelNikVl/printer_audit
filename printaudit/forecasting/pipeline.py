"""Оркестрация: строит ряд -> считает прогноз/бэктест -> сохраняет
ForecastRun. Отдельно — прогноз исчерпания расходника и риск простоя для
устройства (не завязаны на дневной ряд заданий печати).

Upsert ЯВНЫЙ (запрос-затем-запись), а не полагается на UniqueConstraint
уровня БД: для scope_type=organization scope_id всегда NULL, а NULL != NULL
в правилах уникальности SQL — значит UNIQUE(scope_type, scope_id, metric,
horizon_days) НЕ защищает от дублей organization-строк (в отличие от
дизайна PrintJob.print_server_id/endpoint_agent_id в Части 3, где именно
это поведение и нужно). Здесь это не нужно вообще — всегда должна быть РОВНО
одна строка на (scope_type, scope_id, metric, horizon_days)."""
import json
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from printaudit.forecasting import (
    FORECAST_MODEL_VERSION,
    HORIZON_DAYS,
    LOAD_METRICS,
    METRIC_DOWNTIME_RISK,
    METRIC_TONER_EXHAUSTION,
    SCOPE_DEVICE,
    SCOPE_ORGANIZATION,
    SCOPE_QUEUE,
    SCOPE_SITE,
)
from printaudit.forecasting.forecast import build_forecast
from printaudit.forecasting.risk import estimate_downtime_risk
from printaudit.forecasting.series import build_daily_series, earliest_activity_date
from printaudit.forecasting.supply import SupplyTrendPoint, estimate_exhaustion
from printaudit.models import (
    ForecastRun,
    PrinterAlert,
    PrinterDevice,
    PrinterHealthSample,
    PrinterQueue,
    PrinterSupplyDailyAgg,
    Site,
)
from printaudit.timeutil import naive_utc, utcnow

logger = logging.getLogger("printaudit.forecasting.pipeline")

HISTORY_LOOKBACK_DAYS = max(HORIZON_DAYS) * 2 + 30  # с запасом под самый требовательный горизонт (90д)
DOWNTIME_RISK_WINDOW_DAYS = 14
SUPPLY_TREND_LOOKBACK_DAYS = 180


def _upsert_forecast_run(session: Session, *, scope_type: str, scope_id: Optional[int], metric: str, horizon_days: int, **fields) -> ForecastRun:
    row = (
        session.query(ForecastRun)
        .filter(
            ForecastRun.scope_type == scope_type, ForecastRun.scope_id == scope_id,
            ForecastRun.metric == metric, ForecastRun.horizon_days == horizon_days,
        )
        .first()
    )
    if row is None:
        row = ForecastRun(scope_type=scope_type, scope_id=scope_id, metric=metric, horizon_days=horizon_days)
        session.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    row.model_version = FORECAST_MODEL_VERSION
    row.computed_at = naive_utc(utcnow())
    return row


def compute_load_forecasts(
    session: Session, scope_type: str, scope_id: Optional[int], now: Optional[date] = None,
) -> List[ForecastRun]:
    """Считает job_count/total_pages/color_pages/bw_pages/cost на всех
    горизонтах (7/30/90) для одного охвата. Использует ИСТОРИЮ ДЛИНОЙ
    HISTORY_LOOKBACK_DAYS для ВСЕХ горизонтов (не только под самый большой)
    — так backtest каждой модели видит одну и ту же историю независимо от
    горизонта, а `build_forecast` сам решает, хватает ли её для
    конкретного horizon_days (см. printaudit.forecasting.min_history_days)."""
    end_date = now or naive_utc(utcnow()).date()

    earliest = earliest_activity_date(session, scope_type, scope_id)
    # Окно НЕ длиннее реального срока наблюдения этого охвата -- иначе
    # build_daily_series честно вернёт нули за годы "до его существования",
    # а history_days_used перестанет отражать реальную историю (см.
    # docstring earliest_activity_date).
    span_days = (end_date - earliest).days if earliest else 0
    window_days = min(HISTORY_LOOKBACK_DAYS, max(span_days, 0))

    rows = []
    for metric in LOAD_METRICS:
        history = build_daily_series(session, scope_type, scope_id, metric, end_date=end_date, num_days=window_days) if window_days else []
        for horizon in HORIZON_DAYS:
            # Одна и та же (вся доступная) история для каждого горизонта --
            # backtest (min_train_size=horizon_days) сам определяет, сколько
            # проверочных точек из неё получится, более длинная история
            # только добавляет точек проверки, не искажает выбор модели.
            result = build_forecast(history, horizon)
            row = _upsert_forecast_run(
                session, scope_type=scope_type, scope_id=scope_id, metric=metric, horizon_days=horizon,
                model_name=result.model_name, history_days_used=result.history_days_used,
                wape=result.wape, mae=result.mae, insufficient_history=result.insufficient_history,
                forecast_json=json.dumps(_forecast_dates(result, end_date)),
            )
            rows.append(row)
    return rows


def _forecast_dates(result, end_date: date) -> dict:
    payload = result.to_json_dict()
    payload["dates"] = [(end_date + timedelta(days=i)).isoformat() for i in range(len(result.values))]
    return payload


def compute_toner_exhaustion(session: Session, device: PrinterDevice, now: Optional[date] = None) -> List[ForecastRun]:
    """Одна строка ForecastRun на каждый supply_type устройства, с
    horizon_days=90 (фиксированная "метка" -- сама оценка не привязана к
    горизонту, это дата, а не ряд точек, но ForecastRun требует
    horizon_days для уникальности; 90 выбран как "долгосрочный", отдельно
    от load-метрик)."""
    end_date = now or naive_utc(utcnow()).date()
    cutoff = end_date - timedelta(days=SUPPLY_TREND_LOOKBACK_DAYS)
    agg_rows = (
        session.query(PrinterSupplyDailyAgg)
        .filter(PrinterSupplyDailyAgg.printer_device_id == device.id, PrinterSupplyDailyAgg.day >= cutoff)
        .order_by(PrinterSupplyDailyAgg.day)
        .all()
    )
    by_type: dict = {}
    for r in agg_rows:
        if r.avg_level_percent is None:
            continue
        by_type.setdefault(r.supply_type, []).append(SupplyTrendPoint(day=r.day, level_percent=r.avg_level_percent))

    rows = []
    for supply_type, points in by_type.items():
        estimate = estimate_exhaustion(points, today=end_date)
        insufficient = estimate is None
        payload = {"insufficient_history": insufficient, "supply_type": supply_type}
        if estimate is not None:
            payload.update(
                exhaustion_date=estimate["exhaustion_date"].isoformat(),
                slope_percent_per_day=estimate["slope_percent_per_day"],
                current_level_percent=estimate["current_level_percent"],
            )
        row = _upsert_forecast_run(
            session, scope_type=SCOPE_DEVICE, scope_id=device.id, metric=f"{METRIC_TONER_EXHAUSTION}:{supply_type}",
            horizon_days=90, model_name="linear_trend" if not insufficient else None,
            history_days_used=len(points), wape=None, mae=None, insufficient_history=insufficient,
            forecast_json=json.dumps(payload),
        )
        rows.append(row)
    return rows


def compute_downtime_risk(session: Session, device: PrinterDevice, now: Optional[datetime] = None) -> ForecastRun:
    now = now or naive_utc(utcnow())
    window_start = now - timedelta(days=DOWNTIME_RISK_WINDOW_DAYS)
    samples = (
        session.query(PrinterHealthSample)
        .filter(PrinterHealthSample.printer_device_id == device.id, PrinterHealthSample.collected_at >= window_start)
        .all()
    )
    unavailable = sum(
        1 for s in samples if s.is_reachable is False or (s.device_status in ("error", "offline"))
    )
    active_errors = (
        session.query(PrinterAlert)
        .filter(
            PrinterAlert.printer_device_id == device.id, PrinterAlert.resolved_at.is_(None),
            PrinterAlert.severity == "critical",
        )
        .count()
    )
    risk = estimate_downtime_risk(unavailable, len(samples), active_errors)
    insufficient = risk is None
    payload = {"insufficient_history": insufficient}
    if risk is not None:
        payload.update(
            level=risk.level, unavailable_ratio=risk.unavailable_ratio,
            active_error_alert_count=risk.active_error_alert_count, sample_count=risk.sample_count,
        )
    return _upsert_forecast_run(
        session, scope_type=SCOPE_DEVICE, scope_id=device.id, metric=METRIC_DOWNTIME_RISK, horizon_days=7,
        model_name="availability_heuristic" if not insufficient else None, history_days_used=len(samples),
        wape=None, mae=None, insufficient_history=insufficient, forecast_json=json.dumps(payload),
    )


def compute_all_forecasts(session: Session, now: Optional[datetime] = None) -> dict:
    """Полный прогон: по каждому активному устройству (нагрузка + тонер +
    риск простоя), по каждой очереди с хотя бы одним заданием, по каждой
    площадке, и один раз для всей организации. Вызывается по расписанию
    (см. scripts/compute_forecasts.py), не на каждый просмотр страницы."""
    now = now or naive_utc(utcnow())
    today = now.date()
    counts = {"devices": 0, "queues": 0, "sites": 0, "organization": 0}

    devices = session.query(PrinterDevice).filter(PrinterDevice.is_active.is_(True)).all()
    for device in devices:
        compute_load_forecasts(session, SCOPE_DEVICE, device.id, now=today)
        compute_toner_exhaustion(session, device, now=today)
        compute_downtime_risk(session, device, now=now)
        counts["devices"] += 1
        session.flush()

    queue_ids = [r[0] for r in session.query(PrinterQueue.id).all()]
    for queue_id in queue_ids:
        compute_load_forecasts(session, SCOPE_QUEUE, queue_id, now=today)
        counts["queues"] += 1
        session.flush()

    site_ids = [r[0] for r in session.query(Site.id).all()]
    for site_id in site_ids:
        compute_load_forecasts(session, SCOPE_SITE, site_id, now=today)
        counts["sites"] += 1
        session.flush()

    compute_load_forecasts(session, SCOPE_ORGANIZATION, None, now=today)
    counts["organization"] = 1

    session.commit()
    return counts
