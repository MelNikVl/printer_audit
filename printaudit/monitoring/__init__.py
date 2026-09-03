"""Мониторинг физических принтеров, расходников, endpoint-агенты и
прогнозирование — см. docs/PRINTER_MONITORING_FORECASTING.md.

Подпакеты:
  status.py        — вычисление статуса устройства (не хранится "навсегда").
  devices.py        — управляемое создание устройств и связи с очередями,
                       со сплошным audit_log (см. printaudit.audit).
  normalize.py      — общая нормализованная модель показаний
                       (NormalizedDeviceReading и т.п.), НЕ зависящая от
                       источника (Zabbix/SNMP/ручной ввод).
  zabbix_adapter.py  — источник zabbix_api (read-only Zabbix JSON-RPC API).
  snmp_adapter.py    — источник direct_snmp (Printer-MIB + профили OID).
  ingest.py          — запись нормализованных показаний в БД, идемпотентно.
"""
MONITORING_SOURCE_ZABBIX = "zabbix_api"
MONITORING_SOURCE_SNMP = "direct_snmp"
MONITORING_SOURCE_MANUAL = "manual"
MONITORING_SOURCE_DISABLED = "disabled"
MONITORING_SOURCES = (
    MONITORING_SOURCE_ZABBIX,
    MONITORING_SOURCE_SNMP,
    MONITORING_SOURCE_MANUAL,
    MONITORING_SOURCE_DISABLED,
)

DEVICE_STATUS_ONLINE = "online"
DEVICE_STATUS_WARNING = "warning"
DEVICE_STATUS_ERROR = "error"
DEVICE_STATUS_OFFLINE = "offline"
DEVICE_STATUS_UNKNOWN = "unknown"
DEVICE_STATUSES = (
    DEVICE_STATUS_ONLINE,
    DEVICE_STATUS_WARNING,
    DEVICE_STATUS_ERROR,
    DEVICE_STATUS_OFFLINE,
    DEVICE_STATUS_UNKNOWN,
)

SUPPLY_LEVEL_OK = "ok"
SUPPLY_LEVEL_LOW = "low"
SUPPLY_LEVEL_CRITICAL = "critical"
SUPPLY_LEVEL_EMPTY = "empty"
SUPPLY_LEVEL_UNKNOWN = "unknown"
SUPPLY_LEVELS = (SUPPLY_LEVEL_OK, SUPPLY_LEVEL_LOW, SUPPLY_LEVEL_CRITICAL, SUPPLY_LEVEL_EMPTY, SUPPLY_LEVEL_UNKNOWN)

# Пороги для level_percent -> level_status, когда источник отдаёт число, но
# не отдаёт готовый статус (типично для direct_snmp; Zabbix обычно уже
# сообщает и то, и другое). НЕ применяются, если level_percent сам None —
# тогда level_status всегда "unknown", без исключений.
SUPPLY_LOW_THRESHOLD_PERCENT = 20.0
SUPPLY_CRITICAL_THRESHOLD_PERCENT = 5.0


def classify_supply_level(level_percent):
    """level_percent=None -> 'unknown' ВСЕГДА — вызывающий код не должен
    сам решать, что None значит "пусто"."""
    if level_percent is None:
        return SUPPLY_LEVEL_UNKNOWN
    if level_percent <= 0:
        return SUPPLY_LEVEL_EMPTY
    if level_percent <= SUPPLY_CRITICAL_THRESHOLD_PERCENT:
        return SUPPLY_LEVEL_CRITICAL
    if level_percent <= SUPPLY_LOW_THRESHOLD_PERCENT:
        return SUPPLY_LEVEL_LOW
    return SUPPLY_LEVEL_OK
