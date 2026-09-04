"""Вычисление статуса физического устройства — как и
printaudit.sites.compute_status для PrintServer, статус НЕ хранится
"навсегда": последний сэмпл может устареть (агент/Zabbix перестал
опрашивать), и тогда устройство должно считаться offline, даже если
последний известный сэмпл был "online"."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from printaudit.monitoring import DEVICE_STATUS_OFFLINE, DEVICE_STATUS_UNKNOWN

# Сэмпл старше этого считается неактуальным -- статус "offline", независимо
# от того, что там было записано (агент/Zabbix мог остановиться, это не
# то же самое, что "принтер подтверждённо недоступен").
STALE_THRESHOLD_MINUTES = 30


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def compute_device_status(latest_sample, now: Optional[datetime] = None) -> str:
    """latest_sample — PrinterHealthSample или None. Возвращает одно из
    DEVICE_STATUSES. Правила (по порядку):
      1. Сэмплов вообще не было -> "unknown".
      2. Сэмпл устарел (> STALE_THRESHOLD_MINUTES) -> "offline" (не доверяем
         старым данным, а не молчим на этот счёт).
      3. is_reachable=False -> "offline" (перекрывает device_status сэмпла).
      4. Иначе -- device_status сэмпла как есть (адаптер уже учёл замятия/
         крышку/аппаратные ошибки при его формировании, см.
         printaudit.monitoring.normalize)."""
    if latest_sample is None:
        return DEVICE_STATUS_UNKNOWN

    now = _naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    collected_at = _naive_utc(latest_sample.collected_at)
    if collected_at is not None and now - collected_at > timedelta(minutes=STALE_THRESHOLD_MINUTES):
        return DEVICE_STATUS_OFFLINE

    if latest_sample.is_reachable is False:
        return DEVICE_STATUS_OFFLINE

    return latest_sample.device_status or DEVICE_STATUS_UNKNOWN
