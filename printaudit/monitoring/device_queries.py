"""Запросы для /printers (список устройств с фильтрами) и /printers/{id}
(карточка устройства) — держим SQL-логику отдельно от тонких роутов
webapp/printers_routes.py, тот же принцип, что и printaudit.queries для
отчётов по заданиям печати.

"Последний" сэмпл на устройство/расходник получается через подзапрос
max(id) с группировкой — портируемо между SQLite и PostgreSQL (в отличие
от оконных функций, которых в кодовой базе до сих пор избегали)."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from printaudit.models import (
    ForecastRun,
    PrinterAlert,
    PrinterCounterSample,
    PrinterDevice,
    PrinterDeviceQueueLink,
    PrinterHealthSample,
    PrinterQueue,
    PrinterSupplyDailyAgg,
    PrinterSupplySample,
    PrintJob,
)
from printaudit.monitoring.status import compute_device_status
from printaudit.timeutil import naive_utc, utcnow


def _latest_by_device(session: Session, model, device_ids: List[int], extra_group_cols=()):
    """Общий шаблон "последняя строка на устройство [+доп.группировка]" —
    подзапрос max(id) сгруппированный по (printer_device_id, *extra_group_cols),
    затем join обратно на саму таблицу по найденным id."""
    if not device_ids:
        return []
    group_cols = (model.printer_device_id, *extra_group_cols)
    subq = (
        session.query(*group_cols, func.max(model.id).label("max_id"))
        .filter(model.printer_device_id.in_(device_ids))
        .group_by(*group_cols)
        .subquery()
    )
    return session.query(model).join(subq, model.id == subq.c.max_id).all()


def latest_health_by_device(session: Session, device_ids: List[int]) -> Dict[int, PrinterHealthSample]:
    rows = _latest_by_device(session, PrinterHealthSample, device_ids)
    return {r.printer_device_id: r for r in rows}


def latest_counter_by_device(session: Session, device_ids: List[int]) -> Dict[int, PrinterCounterSample]:
    rows = _latest_by_device(session, PrinterCounterSample, device_ids)
    return {r.printer_device_id: r for r in rows}


def latest_supplies_by_device(session: Session, device_ids: List[int]) -> Dict[int, List[PrinterSupplySample]]:
    rows = _latest_by_device(session, PrinterSupplySample, device_ids, extra_group_cols=(PrinterSupplySample.supply_type,))
    result: Dict[int, List[PrinterSupplySample]] = {}
    for r in rows:
        result.setdefault(r.printer_device_id, []).append(r)
    return result


def active_alert_counts(session: Session, device_ids: List[int]) -> Dict[int, int]:
    if not device_ids:
        return {}
    rows = (
        session.query(PrinterAlert.printer_device_id, func.count(PrinterAlert.id))
        .filter(PrinterAlert.printer_device_id.in_(device_ids), PrinterAlert.resolved_at.is_(None))
        .group_by(PrinterAlert.printer_device_id)
        .all()
    )
    return dict(rows)


def linked_queue_ids_by_device(session: Session, device_ids: List[int]) -> Dict[int, List[int]]:
    if not device_ids:
        return {}
    rows = (
        session.query(PrinterDeviceQueueLink.printer_device_id, PrinterDeviceQueueLink.printer_queue_id)
        .filter(PrinterDeviceQueueLink.printer_device_id.in_(device_ids), PrinterDeviceQueueLink.is_active.is_(True))
        .all()
    )
    result: Dict[int, List[int]] = {}
    for device_id, queue_id in rows:
        result.setdefault(device_id, []).append(queue_id)
    return result


def job_totals_for_queues(session: Session, queue_ids: List[int], since: datetime) -> Dict[int, dict]:
    """{queue_id: {"jobs": N, "pages": N}} за период -- ИЗ PrintJob (уже
    посчитанные/тарифицированные задания), а не из аппаратных счётчиков
    принтера (PrinterCounterSample — отдельная телеметрия для трендов/
    прогноза, не источник истины по объёму печати, см.
    docs/PRINTER_MONITORING_FORECASTING.md)."""
    if not queue_ids:
        return {}
    rows = (
        session.query(PrintJob.printer_queue_id, func.count(PrintJob.id), func.coalesce(func.sum(PrintJob.total_pages), 0))
        .filter(PrintJob.printer_queue_id.in_(queue_ids), PrintJob.time_created >= since)
        .group_by(PrintJob.printer_queue_id)
        .all()
    )
    return {qid: {"jobs": jobs, "pages": pages} for qid, jobs, pages in rows}


@dataclass
class DeviceRow:
    device: PrinterDevice
    status: str
    latest_health: Optional[PrinterHealthSample]
    supplies: List[PrinterSupplySample] = field(default_factory=list)
    active_alert_count: int = 0
    linked_queue_count: int = 0
    jobs_period: int = 0
    pages_period: int = 0


def list_devices(
    session: Session, *, site_id: Optional[int] = None, print_server_id: Optional[int] = None,
    status: Optional[str] = None, model: Optional[str] = None, monitoring_source: Optional[str] = None,
    has_active_errors: Optional[bool] = None, low_supply_only: Optional[bool] = None,
    no_data_only: Optional[bool] = None, q: Optional[str] = None, period_days: int = 30,
) -> List[DeviceRow]:
    query = session.query(PrinterDevice).filter(PrinterDevice.is_active.is_(True))
    if site_id:
        query = query.filter(PrinterDevice.site_id == site_id)
    if print_server_id:
        query = query.filter(PrinterDevice.print_server_id == print_server_id)
    if model:
        query = query.filter(PrinterDevice.model == model)
    if monitoring_source:
        query = query.filter(PrinterDevice.monitoring_source == monitoring_source)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(PrinterDevice.display_name.ilike(like), PrinterDevice.hostname.ilike(like), PrinterDevice.ip_address.ilike(like))
        )
    devices = query.order_by(PrinterDevice.display_name).all()
    device_ids = [d.id for d in devices]

    latest_health = latest_health_by_device(session, device_ids)
    latest_supplies = latest_supplies_by_device(session, device_ids)
    alert_counts = active_alert_counts(session, device_ids)
    queue_links = linked_queue_ids_by_device(session, device_ids)
    all_queue_ids = [qid for ids in queue_links.values() for qid in ids]
    since = naive_utc(utcnow()) - timedelta(days=period_days)
    job_totals = job_totals_for_queues(session, all_queue_ids, since)

    now = naive_utc(utcnow())
    rows = []
    for d in devices:
        health = latest_health.get(d.id)
        computed_status = compute_device_status(health, now=now)
        if status and computed_status != status:
            continue

        supplies = latest_supplies.get(d.id, [])
        alert_count = alert_counts.get(d.id, 0)
        if has_active_errors and alert_count == 0:
            continue
        if low_supply_only and not any(s.level_status in ("low", "critical", "empty") for s in supplies):
            continue
        if no_data_only and health is not None:
            continue

        queue_ids = queue_links.get(d.id, [])
        jobs = sum(job_totals.get(qid, {}).get("jobs", 0) for qid in queue_ids)
        pages = sum(job_totals.get(qid, {}).get("pages", 0) for qid in queue_ids)

        rows.append(
            DeviceRow(
                device=d, status=computed_status, latest_health=health, supplies=supplies,
                active_alert_count=alert_count, linked_queue_count=len(queue_ids), jobs_period=jobs, pages_period=pages,
            )
        )
    return rows


@dataclass
class DashboardSummary:
    total_devices: int = 0
    online: int = 0
    warning: int = 0
    error: int = 0
    offline: int = 0
    unknown: int = 0
    low_toner_count: int = 0
    active_errors_count: int = 0
    no_data_count: int = 0
    sites_with_problems: int = 0


def dashboard_summary(session: Session, *, site_id: Optional[int] = None) -> DashboardSummary:
    rows = list_devices(session, site_id=site_id)
    summary = DashboardSummary(total_devices=len(rows))
    status_counts = {"online": 0, "warning": 0, "error": 0, "offline": 0, "unknown": 0}
    problem_site_ids = set()
    for row in rows:
        if row.status in status_counts:
            status_counts[row.status] += 1
        if row.latest_health is None:
            summary.no_data_count += 1
        if row.active_alert_count > 0:
            summary.active_errors_count += 1
        if any(s.level_status in ("low", "critical", "empty") for s in row.supplies):
            summary.low_toner_count += 1
        if row.status in ("error", "offline") or row.active_alert_count > 0:
            problem_site_ids.add(row.device.site_id)
    summary.online = status_counts["online"]
    summary.warning = status_counts["warning"]
    summary.error = status_counts["error"]
    summary.offline = status_counts["offline"]
    summary.unknown = status_counts["unknown"]
    summary.sites_with_problems = len(problem_site_ids)
    return summary


def supply_history(session: Session, device_id: int, supply_type: str, days: int) -> List[PrinterSupplyDailyAgg]:
    """Дневной агрегат (не сырые сэмплы -- переживает retention, см.
    printaudit/monitoring/retention.py) для графика тренда расходника."""
    since = (naive_utc(utcnow()) - timedelta(days=days)).date()
    return (
        session.query(PrinterSupplyDailyAgg)
        .filter(
            PrinterSupplyDailyAgg.printer_device_id == device_id, PrinterSupplyDailyAgg.supply_type == supply_type,
            PrinterSupplyDailyAgg.day >= since,
        )
        .order_by(PrinterSupplyDailyAgg.day)
        .all()
    )


def counter_history(session: Session, device_id: int, days: int, limit: int = 500) -> List[PrinterCounterSample]:
    since = naive_utc(utcnow()) - timedelta(days=days)
    return (
        session.query(PrinterCounterSample)
        .filter(PrinterCounterSample.printer_device_id == device_id, PrinterCounterSample.collected_at >= since)
        .order_by(PrinterCounterSample.collected_at)
        .limit(limit)
        .all()
    )


def alerts_for_device(session: Session, device_id: int, resolved_lookback_days: int = 30, limit: int = 100) -> List[PrinterAlert]:
    since = naive_utc(utcnow()) - timedelta(days=resolved_lookback_days)
    return (
        session.query(PrinterAlert)
        .filter(
            PrinterAlert.printer_device_id == device_id,
            or_(PrinterAlert.resolved_at.is_(None), PrinterAlert.resolved_at >= since),
        )
        .order_by(PrinterAlert.resolved_at.is_(None).desc(), PrinterAlert.opened_at.desc())
        .limit(limit)
        .all()
    )


def forecasts_for_device(session: Session, device_id: int) -> List[ForecastRun]:
    return (
        session.query(ForecastRun)
        .filter(ForecastRun.scope_type == "device", ForecastRun.scope_id == device_id)
        .order_by(ForecastRun.metric, ForecastRun.horizon_days)
        .all()
    )


@dataclass
class LinkedQueueRow:
    link: PrinterDeviceQueueLink
    queue: PrinterQueue
    jobs_period: int = 0
    pages_period: int = 0


def linked_queues_for_device(session: Session, device_id: int, period_days: int = 30) -> List[LinkedQueueRow]:
    links = (
        session.query(PrinterDeviceQueueLink)
        .filter(PrinterDeviceQueueLink.printer_device_id == device_id, PrinterDeviceQueueLink.is_active.is_(True))
        .all()
    )
    queue_ids = [link.printer_queue_id for link in links]
    queues_by_id = {q.id: q for q in session.query(PrinterQueue).filter(PrinterQueue.id.in_(queue_ids)).all()} if queue_ids else {}
    since = naive_utc(utcnow()) - timedelta(days=period_days)
    totals = job_totals_for_queues(session, queue_ids, since)
    return [
        LinkedQueueRow(
            link=link, queue=queues_by_id[link.printer_queue_id],
            jobs_period=totals.get(link.printer_queue_id, {}).get("jobs", 0),
            pages_period=totals.get(link.printer_queue_id, {}).get("pages", 0),
        )
        for link in links if link.printer_queue_id in queues_by_id
    ]


@dataclass
class DeviceDetail:
    device: PrinterDevice
    status: str
    latest_health: Optional[PrinterHealthSample]
    latest_counter: Optional[PrinterCounterSample]
    supplies: List[PrinterSupplySample]
    alerts: List[PrinterAlert]
    linked_queues: List[LinkedQueueRow]
    forecasts: List[ForecastRun]
    counter_history_points: List[PrinterCounterSample]
    jobs_period: int
    pages_period: int


def get_device_detail(session: Session, device_id: int, period_days: int = 30, history_days: int = 90) -> Optional[DeviceDetail]:
    device = session.get(PrinterDevice, device_id)
    if device is None:
        return None

    now = naive_utc(utcnow())
    latest_health = latest_health_by_device(session, [device_id]).get(device_id)
    latest_counter = latest_counter_by_device(session, [device_id]).get(device_id)
    supplies = latest_supplies_by_device(session, [device_id]).get(device_id, [])
    linked_queues = linked_queues_for_device(session, device_id, period_days=period_days)

    return DeviceDetail(
        device=device, status=compute_device_status(latest_health, now=now), latest_health=latest_health,
        latest_counter=latest_counter, supplies=supplies, alerts=alerts_for_device(session, device_id),
        linked_queues=linked_queues, forecasts=forecasts_for_device(session, device_id),
        counter_history_points=counter_history(session, device_id, days=history_days),
        jobs_period=sum(r.jobs_period for r in linked_queues), pages_period=sum(r.pages_period for r in linked_queues),
    )
