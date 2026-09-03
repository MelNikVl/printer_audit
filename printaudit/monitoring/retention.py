"""Retention для сырых сэмплов мониторинга (см.
docs/PRINTER_MONITORING_FORECASTING.md): сырые данные хранятся
ограниченный период (RAW_RETENTION_DAYS), затем удаляются — но перед
удалением уровни расходников агрегируются в printer_supply_daily_agg
(мин/среднее/макс за день), который остаётся надолго (один ряд в день на
пару устройство+расходник — компактно даже за годы).

Health/counter-сэмплы старше окна удаляются БЕЗ отдельной агрегации в этом
MVP (последний известный статус уже кэшируется в
PrinterDevice.last_status/last_seen_at) — осознанное ограничение MVP, не
пропущенный случай, см. docs/PRINTER_MONITORING_FORECASTING.md."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from printaudit.models import (
    PrinterAlert,
    PrinterCounterSample,
    PrinterHealthSample,
    PrinterSupplyDailyAgg,
    PrinterSupplySample,
)
from printaudit.timeutil import naive_utc

RAW_RETENTION_DAYS = 30
# Только РЕШЁННЫЕ алерты когда-либо удаляются, и то заметно позже сырых
# сэмплов -- история проблем ценнее, легковесна, и активные (resolved_at
# IS NULL) не удаляются никогда, независимо от возраста.
RESOLVED_ALERT_RETENTION_DAYS = 180


def _cutoff(days: int, now: Optional[datetime] = None) -> datetime:
    now = naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    return now - timedelta(days=days)


def aggregate_supply_samples_to_daily(session: Session, before: Optional[datetime] = None) -> int:
    """Строит/дополняет printer_supply_daily_agg по printer_supply_samples
    СТАРШЕ `before` (по умолчанию — RAW_RETENTION_DAYS назад). Идемпотентно:
    повторный вызов на тех же исходных сэмплах пересчитывает ту же строку
    (UNIQUE(printer_device_id, supply_type, day)), не создаёт дублей.
    Сэмплы с level_percent=None (неизвестно) в агрегат не идут — усреднять
    "неизвестно" бессмысленно, sample_count в этом случае просто ниже."""
    before = before or _cutoff(RAW_RETENTION_DAYS)

    samples = (
        session.query(PrinterSupplySample)
        .filter(PrinterSupplySample.collected_at < before, PrinterSupplySample.level_percent.isnot(None))
        .all()
    )
    by_key: dict = {}
    for sample in samples:
        collected_at = naive_utc(sample.collected_at) or sample.collected_at
        key = (sample.printer_device_id, sample.supply_type, collected_at.date())
        by_key.setdefault(key, []).append(sample.level_percent)

    written = 0
    for (device_id, supply_type, day), levels in by_key.items():
        existing = (
            session.query(PrinterSupplyDailyAgg)
            .filter_by(printer_device_id=device_id, supply_type=supply_type, day=day)
            .first()
        )
        if existing is None:
            existing = PrinterSupplyDailyAgg(printer_device_id=device_id, supply_type=supply_type, day=day)
            session.add(existing)
        existing.min_level_percent = min(levels)
        existing.max_level_percent = max(levels)
        existing.avg_level_percent = sum(levels) / len(levels)
        existing.sample_count = len(levels)
        written += 1
    return written


def purge_raw_samples(session: Session, before: Optional[datetime] = None) -> dict:
    """Удаляет сырые health/counter/supply сэмплы старше `before`. ВСЕГДА
    вызывать aggregate_supply_samples_to_daily() с теми же (или более
    старыми) границами ПЕРЕД этим — иначе тренд расходника необратимо
    теряется (см. run_retention, который делает это в правильном порядке)."""
    before = before or _cutoff(RAW_RETENTION_DAYS)
    return {
        "health": (
            session.query(PrinterHealthSample)
            .filter(PrinterHealthSample.collected_at < before)
            .delete(synchronize_session=False)
        ),
        "counter": (
            session.query(PrinterCounterSample)
            .filter(PrinterCounterSample.collected_at < before)
            .delete(synchronize_session=False)
        ),
        "supply": (
            session.query(PrinterSupplySample)
            .filter(PrinterSupplySample.collected_at < before)
            .delete(synchronize_session=False)
        ),
    }


def purge_resolved_alerts(session: Session, before: Optional[datetime] = None) -> int:
    """Удаляет ТОЛЬКО решённые (resolved_at IS NOT NULL) алерты старше
    RESOLVED_ALERT_RETENTION_DAYS. Активные — никогда, ни при каком
    возрасте opened_at."""
    before = before or _cutoff(RESOLVED_ALERT_RETENTION_DAYS)
    return (
        session.query(PrinterAlert)
        .filter(PrinterAlert.resolved_at.isnot(None), PrinterAlert.resolved_at < before)
        .delete(synchronize_session=False)
    )


def run_retention(session: Session, now: Optional[datetime] = None) -> dict:
    """Единая точка входа для scripts/monitoring_retention.py (Task
    Scheduler, раз в сутки) — агрегирует расходники, затем чистит сырые
    данные и старые решённые алерты, в этом порядке, одной транзакцией."""
    raw_cutoff = _cutoff(RAW_RETENTION_DAYS, now)
    aggregated = aggregate_supply_samples_to_daily(session, before=raw_cutoff)
    purged = purge_raw_samples(session, before=raw_cutoff)
    purged_alerts = purge_resolved_alerts(session, before=_cutoff(RESOLVED_ALERT_RETENTION_DAYS, now))
    session.commit()
    return {"aggregated_supply_days": aggregated, "purged_alerts": purged_alerts, **purged}
