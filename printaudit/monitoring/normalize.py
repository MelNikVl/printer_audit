"""Единая нормализованная модель показаний устройства — то, что оба
адаптера (Zabbix, direct SNMP) обязаны производить, независимо от
источника. printaudit.monitoring.ingest пишет ТОЛЬКО эту форму в БД, не
зная деталей ни Zabbix API, ни SNMP OID."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class NormalizedSupplyReading:
    supply_type: str
    # None = источник не смог определить уровень (не 0!). level_status,
    # если не задан явно источником, вычисляется из level_percent через
    # printaudit.monitoring.classify_supply_level при записи в БД.
    level_percent: Optional[float] = None
    level_status: Optional[str] = None


@dataclass
class NormalizedAlertReading:
    alert_type: str
    severity: str = "warning"
    message: Optional[str] = None
    # Стабильный идентификатор проблемы у ИСТОЧНИКА (Zabbix eventid — новый
    # инцидент того же типа получает новый external_id, что и должно
    # переоткрыть алерт заново; для direct_snmp, у которого нет отдельного
    # "id проблемы", адаптер обязан передать сюда сам alert_type как
    # стабильный псевдо-id — иначе NULL не будет ловить дубликаты при
    # повторном опросе, см. printaudit.monitoring.ingest).
    external_id: str = ""


@dataclass
class NormalizedDeviceReading:
    collected_at: datetime
    source: str  # zabbix_api | direct_snmp | manual

    is_reachable: Optional[bool] = None
    device_status: str = "unknown"
    has_paper_jam: Optional[bool] = None
    has_cover_open: Optional[bool] = None
    has_paper_out: Optional[bool] = None
    has_hardware_error: Optional[bool] = None
    raw_status_text: Optional[str] = None

    total_pages: Optional[int] = None
    color_pages: Optional[int] = None
    bw_pages: Optional[int] = None

    supplies: List[NormalizedSupplyReading] = field(default_factory=list)
    # Список ТЕКУЩИХ активных проблем на момент опроса (не дельта) —
    # printaudit.monitoring.ingest сам закрывает алерты, отсутствующие в
    # этом списке при очередном опросе того же источника.
    alerts: List[NormalizedAlertReading] = field(default_factory=list)
