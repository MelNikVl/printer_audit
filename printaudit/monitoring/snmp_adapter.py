"""direct_snmp адаптер — опрашивает устройство напрямую по SNMP С СЕРВЕРА
ПЛОЩАДКИ (никогда из центра — центр не открывает входящих SNMP-соединений
к площадкам, см. docs/PRINTER_MONITORING_FORECASTING.md). Printer-MIB
(RFC 3805) как база; vendor-specific OID переопределяются через
SnmpProfile.oid_map_json.

Реальный опрос делает pysnmp — ОПЦИОНАЛЬНАЯ зависимость (см.
requirements.txt), импортируется лениво внутри _default_snmp_get: площадки,
которые вообще не используют direct_snmp (только zabbix_api/manual), не
обязаны её ставить. Логика нормализации протестирована через инъекцию
низкоуровневого `getter` — без сети и без pysnmp (см. tests/test_snmp_adapter.py)."""
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from printaudit.monitoring import MONITORING_SOURCE_SNMP
from printaudit.monitoring.normalize import NormalizedAlertReading, NormalizedDeviceReading, NormalizedSupplyReading

logger = logging.getLogger("printaudit.monitoring.snmp")

# Printer-MIB (RFC 3805) — действительны для подавляющего большинства
# принтеров вне зависимости от вендора. total_pages/toner_black — самые
# распространённые OID; профиль конкретной модели может переопределить их
# и добавить остальные цвета/расходники (см. SnmpProfile.oid_map_json).
DEFAULT_OIDS = {
    "device_status": "1.3.6.1.2.1.25.3.5.1.1.1",  # hrPrinterStatus
    "total_pages": "1.3.6.1.2.1.43.10.2.1.4.1.1",  # prtMarkerLifeCount
    "toner_black": "1.3.6.1.2.1.43.11.1.1.9.1.1",  # prtMarkerSuppliesLevel (индекс расходника 1)
}

SUPPLY_OID_FIELDS = ("toner_black", "toner_cyan", "toner_magenta", "toner_yellow", "drum", "waste_toner")

# hrPrinterStatus (RFC 1514/2790): 1=other 2=unknown 3=idle 4=printing 5=warmup.
_HR_PRINTER_STATUS_MAP = {"3": "online", "4": "online", "5": "online", "1": "warning", "2": "unknown"}


class SnmpPollError(RuntimeError):
    pass


def _default_snmp_get(host: str, port: int, community: str, oid: str, timeout: float, retries: int) -> Optional[str]:
    """Реальный SNMP GET через pysnmp — см. docstring модуля про ленивый
    импорт. НЕ покрыт тестами напрямую (нет реального железа/pysnmp в CI);
    вся протестированная логика — в poll_device через инъекцию getter."""
    try:
        from pysnmp.hlapi import (  # type: ignore
            CommunityData, ContextData, ObjectIdentity, ObjectType,
            SnmpEngine, UdpTransportTarget, getCmd,
        )
    except ImportError as exc:  # pragma: no cover - опциональная зависимость
        raise SnmpPollError(
            "monitoring_source=direct_snmp настроен, но пакет pysnmp не установлен. "
            "Выполните: pip install pysnmp (см. docs/PRINTER_MONITORING_FORECASTING.md)."
        ) from exc

    iterator = getCmd(
        SnmpEngine(),
        CommunityData(community),
        UdpTransportTarget((host, port), timeout=timeout, retries=retries),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    error_indication, error_status, _error_index, var_binds = next(iterator)
    if error_indication or error_status:
        raise SnmpPollError(f"SNMP GET {oid}@{host} не удался: {error_indication or error_status}")
    return str(var_binds[0][1])


def resolve_snmp_credential(profile) -> str:
    """Читает community/учётные данные из переменной окружения, ИМЯ которой
    задано в SnmpProfile.credentials_env_var — сам секрет никогда не
    хранится в БД (тот же принцип, что и AD_BIND_PASSWORD/AGENT_TOKEN)."""
    import os

    if profile is None or not profile.credentials_env_var:
        return "public"
    return os.environ.get(profile.credentials_env_var) or "public"


def _parse_oid_map(profile) -> dict:
    oid_map = dict(DEFAULT_OIDS)
    if profile and profile.oid_map_json:
        import json

        try:
            extra = json.loads(profile.oid_map_json) or {}
            if isinstance(extra, dict):
                oid_map.update(extra)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "SnmpProfile %s: невалидный oid_map_json, использую только значения по умолчанию",
                getattr(profile, "id", None),
            )
    return oid_map


def poll_device(
    device, profile=None, credential: Optional[str] = None, getter: Optional[Callable] = None,
) -> NormalizedDeviceReading:
    """device — printaudit.models.PrinterDevice (нужен ip_address); profile —
    printaudit.models.SnmpProfile или None (тогда используются значения по
    умолчанию — port=161, timeout=2с, retries=1, только Printer-MIB base
    OID). `getter(host, port, community, oid, timeout, retries) -> str|None`
    — низкоуровневая функция; по умолчанию реальный SNMP через pysnmp, в
    тестах — фейк без сети/железа. Один недоступный/неподдерживаемый OID НЕ
    должен провалить весь опрос — соответствующее поле просто останется
    None (не 0/False)."""
    getter = getter or _default_snmp_get
    now = datetime.now(timezone.utc)

    if not getattr(device, "ip_address", None):
        return NormalizedDeviceReading(
            collected_at=now, source=MONITORING_SOURCE_SNMP, is_reachable=None, device_status="unknown",
            raw_status_text="У устройства не задан IP-адрес",
        )

    oid_map = _parse_oid_map(profile)
    port = profile.port if profile else 161
    timeout = profile.timeout_seconds if profile else 2.0
    retries = profile.retries if profile else 1
    community = credential if credential is not None else resolve_snmp_credential(profile)

    def _get(field_name: str) -> Optional[str]:
        oid = oid_map.get(field_name)
        if not oid:
            return None
        try:
            return getter(device.ip_address, port, community, oid, timeout, retries)
        except Exception as exc:  # noqa: BLE001 - один OID не должен убить весь опрос
            logger.info("SNMP GET %s (%s) для устройства %s не удался: %s", field_name, oid, device.id, exc)
            return None

    raw_status = _get("device_status")
    if raw_status is None:
        return NormalizedDeviceReading(
            collected_at=now, source=MONITORING_SOURCE_SNMP, is_reachable=False, device_status="offline",
            raw_status_text="Устройство не ответило по SNMP",
        )

    device_status = _HR_PRINTER_STATUS_MAP.get(str(raw_status).strip(), "unknown")

    total_pages_raw = _get("total_pages")
    total_pages = None
    if total_pages_raw is not None:
        try:
            total_pages = int(float(total_pages_raw))
        except (TypeError, ValueError):
            total_pages = None

    supplies = []
    for supply_type in SUPPLY_OID_FIELDS:
        if supply_type not in oid_map:
            continue
        raw_level = _get(supply_type)
        level_percent = None
        if raw_level is not None:
            try:
                level_percent = float(raw_level)
            except (TypeError, ValueError):
                level_percent = None
        supplies.append(NormalizedSupplyReading(supply_type=supply_type, level_percent=level_percent))

    alerts = []
    if str(raw_status).strip() == "1":  # hrPrinterStatus=other — что-то не так, но неизвестно что именно
        alerts.append(
            NormalizedAlertReading(
                alert_type="hardware_error", severity="warning", external_id="hardware_error",
                message="hrPrinterStatus=other (устройство сообщает о проблеме без деталей)",
            )
        )

    return NormalizedDeviceReading(
        collected_at=now, source=MONITORING_SOURCE_SNMP, is_reachable=True, device_status=device_status,
        raw_status_text=f"hrPrinterStatus={raw_status}", total_pages=total_pages, supplies=supplies, alerts=alerts,
    )
