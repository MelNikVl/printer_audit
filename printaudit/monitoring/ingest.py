"""Запись NormalizedDeviceReading (см. printaudit.monitoring.normalize) в
БД — идемпотентно (UNIQUE(printer_device_id, collected_at, source[, ...])
на каждой таблице сэмплов, см. printaudit.models) и с примирением активных
алертов (открыть новые, закрыть пропавшие из текущего опроса)."""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from printaudit.models import (
    PrinterAlert,
    PrinterCounterSample,
    PrinterDevice,
    PrinterHealthSample,
    PrinterSupplySample,
)
from printaudit.monitoring import classify_supply_level
from printaudit.timeutil import naive_utc, utcnow


def _round_to_minute(dt: datetime) -> datetime:
    """Сглаживает мелкие различия во времени между попытками одного и того
    же логического опроса (сеть/таймауты могут сдвинуть collected_at на
    секунды) до стабильного идемпотентного ключа."""
    dt = naive_utc(dt) or dt
    return dt.replace(second=0, microsecond=0)


def ingest_reading(
    session: Session,
    device: PrinterDevice,
    reading,
    monitoring_run_id: Optional[int] = None,
) -> None:
    collected_at = _round_to_minute(reading.collected_at)

    existing_health = (
        session.query(PrinterHealthSample)
        .filter_by(printer_device_id=device.id, collected_at=collected_at, source=reading.source)
        .first()
    )
    if existing_health is None:
        session.add(
            PrinterHealthSample(
                printer_device_id=device.id, monitoring_run_id=monitoring_run_id, collected_at=collected_at,
                source=reading.source, is_reachable=reading.is_reachable, device_status=reading.device_status,
                has_paper_jam=reading.has_paper_jam, has_cover_open=reading.has_cover_open,
                has_paper_out=reading.has_paper_out, has_hardware_error=reading.has_hardware_error,
                raw_status_text=reading.raw_status_text,
            )
        )

    if reading.total_pages is not None or reading.color_pages is not None or reading.bw_pages is not None:
        existing_counter = (
            session.query(PrinterCounterSample)
            .filter_by(printer_device_id=device.id, collected_at=collected_at, source=reading.source)
            .first()
        )
        if existing_counter is None:
            session.add(
                PrinterCounterSample(
                    printer_device_id=device.id, monitoring_run_id=monitoring_run_id, collected_at=collected_at,
                    source=reading.source, total_pages=reading.total_pages,
                    color_pages=reading.color_pages, bw_pages=reading.bw_pages,
                )
            )

    for supply in reading.supplies:
        existing_supply = (
            session.query(PrinterSupplySample)
            .filter_by(
                printer_device_id=device.id, collected_at=collected_at, source=reading.source,
                supply_type=supply.supply_type,
            )
            .first()
        )
        if existing_supply is None:
            level_status = supply.level_status or classify_supply_level(supply.level_percent)
            session.add(
                PrinterSupplySample(
                    printer_device_id=device.id, monitoring_run_id=monitoring_run_id, collected_at=collected_at,
                    source=reading.source, supply_type=supply.supply_type,
                    level_percent=supply.level_percent, level_status=level_status,
                )
            )

    _reconcile_alerts(session, device, reading)

    device.last_seen_at = collected_at
    device.last_status = reading.device_status
    device.updated_at = utcnow()


def _reconcile_alerts(session: Session, device: PrinterDevice, reading) -> None:
    """Открывает новые проблемы, ПЕРЕоткрывает ранее закрытые (та же
    (alert_type, external_id) — обязательно через UPDATE существующей
    строки, не INSERT новой: у direct_snmp external_id стабильно равен
    alert_type, поэтому повторное замятие того же типа после устранения
    предыдущего иначе упёрлось бы в UNIQUE(printer_device_id, alert_type,
    external_id)), закрывает те, что перестали появляться в текущем опросе
    того же источника (сравнение только в пределах ОДНОГО source — Zabbix
    и direct_snmp не должны закрывать алерты друг друга, если оба почему-то
    настроены на одно устройство)."""
    current_by_key = {(a.alert_type, a.external_id): a for a in reading.alerts}

    existing_for_source = (
        session.query(PrinterAlert).filter_by(printer_device_id=device.id, source=reading.source).all()
    )
    existing_by_key = {(a.alert_type, a.external_id): a for a in existing_for_source}

    for alert in existing_for_source:
        key = (alert.alert_type, alert.external_id)
        if alert.resolved_at is None and key not in current_by_key:
            alert.resolved_at = utcnow()
            alert.updated_at = utcnow()

    opened_at = naive_utc(reading.collected_at) or reading.collected_at
    for key, normalized in current_by_key.items():
        existing = existing_by_key.get(key)
        if existing is None:
            session.add(
                PrinterAlert(
                    printer_device_id=device.id, source=reading.source, alert_type=normalized.alert_type,
                    severity=normalized.severity, message=normalized.message, opened_at=opened_at,
                    external_id=normalized.external_id, resolved_at=None,
                )
            )
        elif existing.resolved_at is not None:
            existing.resolved_at = None
            existing.opened_at = opened_at
            existing.severity = normalized.severity
            existing.message = normalized.message
            existing.updated_at = utcnow()
        # else: уже открыт и совпадает по ключу -- ничего не меняем.
