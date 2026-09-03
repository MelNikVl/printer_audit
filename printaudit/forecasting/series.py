"""Строит плотный ежедневный ряд (без пропусков) метрик нагрузки печати для
одного охвата (устройство/очередь/площадка/организация) — сырые строки из
БД, группировка по дню на стороне Python. Тот же приём "портируемости
SQLite/PostgreSQL", что и в printaudit/monitoring/retention.py (там же
объяснение, почему не SQL date()-группировка)."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from printaudit.forecasting import (
    METRIC_BW_PAGES,
    METRIC_COLOR_PAGES,
    METRIC_COST,
    METRIC_JOB_COUNT,
    METRIC_TOTAL_PAGES,
    SCOPE_DEVICE,
    SCOPE_ORGANIZATION,
    SCOPE_QUEUE,
    SCOPE_SITE,
)
from printaudit.models import PrinterDeviceQueueLink, PrintJob

_FIELD_BY_METRIC = {
    METRIC_JOB_COUNT: "job_count",
    METRIC_TOTAL_PAGES: "total_pages",
    METRIC_COLOR_PAGES: "color_pages",
    METRIC_BW_PAGES: "bw_pages",
    METRIC_COST: "cost",
}


@dataclass
class DailyTotals:
    job_count: float = 0.0
    total_pages: float = 0.0
    color_pages: float = 0.0
    bw_pages: float = 0.0
    cost: float = 0.0


def _queue_ids_for_device(session: Session, device_id: int) -> List[int]:
    rows = (
        session.query(PrinterDeviceQueueLink.printer_queue_id)
        .filter(PrinterDeviceQueueLink.printer_device_id == device_id, PrinterDeviceQueueLink.is_active.is_(True))
        .all()
    )
    return [r[0] for r in rows]


def _scoped_query(session: Session, scope_type: str, scope_id: Optional[int]):
    query = session.query(PrintJob.time_created, PrintJob.total_pages, PrintJob.is_color, PrintJob.cost)
    if scope_type == SCOPE_DEVICE:
        queue_ids = _queue_ids_for_device(session, scope_id)
        if not queue_ids:
            return query.filter(PrintJob.id.is_(None))  # нет связанных очередей -- заведомо пустой ряд, не ошибка
        return query.filter(PrintJob.printer_queue_id.in_(queue_ids))
    if scope_type == SCOPE_QUEUE:
        return query.filter(PrintJob.printer_queue_id == scope_id)
    if scope_type == SCOPE_SITE:
        return query.filter(PrintJob.site_id == scope_id)
    if scope_type == SCOPE_ORGANIZATION:
        return query
    raise ValueError(f"Неизвестный scope_type: {scope_type!r}")


def earliest_activity_date(session: Session, scope_type: str, scope_id: Optional[int]) -> Optional[date]:
    """Дата самого раннего задания печати в этом охвате — используется
    вызывающим кодом (printaudit/forecasting/pipeline.py), чтобы НЕ
    заполнять нулями годы "истории" для площадки/устройства, заведённых
    вчера: build_daily_series всегда возвращает плотный ряд запрошенной
    длины, поэтому без этой проверки history_days_used всегда был бы равен
    запрошенному окну, а не реальному сроку наблюдения — и "недостаточно
    данных" перестало бы работать для новых объектов."""
    from sqlalchemy import func

    query = _scoped_query(session, scope_type, scope_id)
    earliest = query.with_entities(func.min(PrintJob.time_created)).scalar()
    return earliest.date() if earliest is not None else None


def build_daily_series(
    session: Session, scope_type: str, scope_id: Optional[int], metric: str, end_date: date, num_days: int,
) -> List[float]:
    """Возвращает СТРОГО num_days значений: индекс 0 — день `end_date -
    num_days`, последний индекс — день перед `end_date` (end_date сам НЕ
    включается — обычно это "сегодня", ещё не завершённый день). Дни без
    заданий — явный 0.0 (это данные, не пропуск: baseline-моделям
    (printaudit/forecasting/models.py) нужен ряд без дыр)."""
    if metric not in _FIELD_BY_METRIC:
        raise ValueError(f"Неизвестная метрика: {metric!r}")
    start_date = end_date - timedelta(days=num_days)
    query = _scoped_query(session, scope_type, scope_id)
    rows = query.filter(PrintJob.time_created >= start_date, PrintJob.time_created < end_date).all()

    buckets: Dict[date, DailyTotals] = {}
    for time_created, total_pages, is_color, cost in rows:
        day = time_created.date()
        totals = buckets.setdefault(day, DailyTotals())
        totals.job_count += 1
        totals.total_pages += total_pages or 0
        if is_color is True:
            totals.color_pages += total_pages or 0
        elif is_color is False:
            totals.bw_pages += total_pages or 0
        totals.cost += cost or 0.0

    field = _FIELD_BY_METRIC[metric]
    series = []
    for i in range(num_days):
        day = start_date + timedelta(days=i)
        totals = buckets.get(day)
        series.append(getattr(totals, field) if totals else 0.0)
    return series
