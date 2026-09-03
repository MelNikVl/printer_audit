"""direct_snmp адаптер — опрашивает устройство напрямую по SNMP С СЕРВЕРА
ПЛОЩАДКИ (никогда из центра — центр не открывает входящих SNMP-соединений
к площадкам, см. docs/PRINTER_MONITORING_FORECASTING.md). Printer-MIB
(RFC 3805) как база; vendor-specific OID переопределяются через
SnmpProfile.oid_map_json.

SNMPv3 (USM: username + auth/priv protocol+key) — предпочтительный и
полностью реализованный режим; SNMPv2c (community) — явный legacy-режим
для устройств, не поддерживающих v3 (осознанный выбор администратора
профиля, не тихий дефолт). НЕТ никакого неявного fallback на
community="public" — если обязательная переменная окружения не задана,
`resolve_snmp_security` бросает `SnmpConfigError` с понятным сообщением, и
опрос ЭТОГО устройства завершается ошибкой (не проваливает весь прогон —
см. collector/monitor_printers.py::_poll_snmp_devices, которая уже
оборачивает опрос каждого устройства в try/except).

Реальный опрос делает pysnmp — ОПЦИОНАЛЬНАЯ зависимость (см.
requirements.txt), импортируется лениво внутри _default_snmp_get: площадки,
которые вообще не используют direct_snmp (только zabbix_api/manual), не
обязаны её ставить. Логика нормализации и разбора конфигурации протестирована
через инъекцию низкоуровневого `getter` — без сети и без pysnmp (см.
tests/test_snmp_adapter.py)."""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Union

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

V3_AUTH_PROTOCOLS = ("MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512")
V3_PRIV_PROTOCOLS = ("DES", "3DES", "AES", "AES192", "AES256")


class SnmpPollError(RuntimeError):
    pass


class SnmpConfigError(SnmpPollError):
    """SnmpProfile настроен неполно/некорректно для выбранной версии SNMP
    (например, snmp_version=v3, но snmp_v3_username не задан, или указанная
    переменная окружения с секретом пуста/отсутствует). НЕТ никакого
    неявного fallback (например, community="public" или анонимный v3) —
    администратор должен явно исправить конфигурацию, а не получить молча
    менее защищённый опрос."""


@dataclass(frozen=True)
class SnmpV2cSecurity:
    community: str


@dataclass(frozen=True)
class SnmpV3Security:
    username: str
    auth_protocol: Optional[str] = None  # None | "MD5" | "SHA" | "SHA224" | "SHA256" | "SHA384" | "SHA512"
    auth_key: Optional[str] = None
    priv_protocol: Optional[str] = None  # None | "DES" | "3DES" | "AES" | "AES192" | "AES256"
    priv_key: Optional[str] = None


SnmpSecurity = Union[SnmpV2cSecurity, SnmpV3Security]


def resolve_snmp_security(profile) -> SnmpSecurity:
    """Собирает параметры безопасности SNMP для опроса устройства — секреты
    ЧИТАЮТСЯ из переменных окружения, имена которых заданы в профиле,
    никогда не хранятся в БД (тот же принцип, что и AD_BIND_PASSWORD/
    AGENT_TOKEN). Бросает SnmpConfigError без какого-либо fallback, если
    профиль не задан, версия неизвестна, или обязательная переменная
    окружения отсутствует/пуста — см. docstring SnmpConfigError."""
    if profile is None:
        raise SnmpConfigError(
            "SNMP-профиль не задан для устройства с monitoring_source=direct_snmp — "
            "невозможно определить версию/учётные данные SNMP (см. PrinterDevice.snmp_profile_id)."
        )

    version = (profile.snmp_version or "").strip().lower()

    if version == "v2c":
        return _resolve_v2c_security(profile)
    if version == "v3":
        return _resolve_v3_security(profile)
    raise SnmpConfigError(
        f"Профиль SNMP '{profile.name}': неизвестный snmp_version={profile.snmp_version!r} "
        "(допустимо только 'v3' или явный legacy 'v2c')."
    )


def _resolve_v2c_security(profile) -> SnmpV2cSecurity:
    env_var = profile.credentials_env_var
    if not env_var:
        raise SnmpConfigError(
            f"Профиль SNMP '{profile.name}': snmp_version=v2c, но не задано credentials_env_var "
            "(имя переменной окружения с community) — без community опрос не выполняется."
        )
    community = os.environ.get(env_var)
    if not community:
        raise SnmpConfigError(
            f"Профиль SNMP '{profile.name}': переменная окружения {env_var} (community для SNMPv2c) "
            "не задана или пуста. Опрос отменён — без неявного fallback на community='public'."
        )
    return SnmpV2cSecurity(community=community)


def _resolve_v3_security(profile) -> SnmpV3Security:
    username = profile.snmp_v3_username
    if not username:
        raise SnmpConfigError(f"Профиль SNMP '{profile.name}': snmp_version=v3, но не задан snmp_v3_username.")

    auth_protocol = (profile.snmp_v3_auth_protocol or "").strip().upper() or None
    if auth_protocol and auth_protocol not in V3_AUTH_PROTOCOLS:
        raise SnmpConfigError(
            f"Профиль SNMP '{profile.name}': неизвестный snmp_v3_auth_protocol={auth_protocol!r} "
            f"(допустимо: {', '.join(V3_AUTH_PROTOCOLS)})."
        )
    auth_key = None
    if auth_protocol:
        if not profile.snmp_v3_auth_key_env_var:
            raise SnmpConfigError(
                f"Профиль SNMP '{profile.name}': задан snmp_v3_auth_protocol={auth_protocol}, но не задано "
                "snmp_v3_auth_key_env_var (имя переменной окружения с ключом аутентификации)."
            )
        auth_key = os.environ.get(profile.snmp_v3_auth_key_env_var)
        if not auth_key:
            raise SnmpConfigError(
                f"Профиль SNMP '{profile.name}': переменная окружения {profile.snmp_v3_auth_key_env_var} "
                "(ключ аутентификации SNMPv3) не задана или пуста."
            )

    priv_protocol = (profile.snmp_v3_priv_protocol or "").strip().upper() or None
    if priv_protocol and priv_protocol not in V3_PRIV_PROTOCOLS:
        raise SnmpConfigError(
            f"Профиль SNMP '{profile.name}': неизвестный snmp_v3_priv_protocol={priv_protocol!r} "
            f"(допустимо: {', '.join(V3_PRIV_PROTOCOLS)})."
        )
    if priv_protocol and not auth_protocol:
        # USM: приватность (шифрование) без аутентификации невозможна —
        # это ограничение самого протокола SNMPv3, не наш выбор.
        raise SnmpConfigError(
            f"Профиль SNMP '{profile.name}': задан snmp_v3_priv_protocol={priv_protocol}, но не задан "
            "snmp_v3_auth_protocol — SNMPv3 (USM) не допускает privacy без authentication."
        )
    priv_key = None
    if priv_protocol:
        if not profile.snmp_v3_priv_key_env_var:
            raise SnmpConfigError(
                f"Профиль SNMP '{profile.name}': задан snmp_v3_priv_protocol={priv_protocol}, но не задано "
                "snmp_v3_priv_key_env_var (имя переменной окружения с ключом приватности)."
            )
        priv_key = os.environ.get(profile.snmp_v3_priv_key_env_var)
        if not priv_key:
            raise SnmpConfigError(
                f"Профиль SNMP '{profile.name}': переменная окружения {profile.snmp_v3_priv_key_env_var} "
                "(ключ приватности SNMPv3) не задана или пуста."
            )

    return SnmpV3Security(
        username=username, auth_protocol=auth_protocol, auth_key=auth_key,
        priv_protocol=priv_protocol, priv_key=priv_key,
    )


def _default_snmp_get(host: str, port: int, security: SnmpSecurity, oid: str, timeout: float, retries: int) -> Optional[str]:
    """Реальный SNMP GET через pysnmp — см. docstring модуля про ленивый
    импорт. НЕ покрыт тестами напрямую (нет реального железа/pysnmp в CI);
    вся протестированная логика — в poll_device/resolve_snmp_security через
    инъекцию getter/фейковых профилей. НИКОГДА не логирует security (может
    содержать auth_key/priv_key/community)."""
    try:
        from pysnmp.hlapi import (  # type: ignore
            CommunityData, ContextData, ObjectIdentity, ObjectType,
            SnmpEngine, UdpTransportTarget, UsmUserData, getCmd,
            usm3DESEDEPrivProtocol, usmAesCfb128Protocol, usmAesCfb192Protocol, usmAesCfb256Protocol,
            usmDESPrivProtocol, usmHMAC128SHA224AuthProtocol, usmHMAC192SHA256AuthProtocol,
            usmHMAC256SHA384AuthProtocol, usmHMAC384SHA512AuthProtocol, usmHMACMD5AuthProtocol,
            usmHMACSHAAuthProtocol, usmNoAuthProtocol, usmNoPrivProtocol,
        )
    except ImportError as exc:  # pragma: no cover - опциональная зависимость
        raise SnmpPollError(
            "monitoring_source=direct_snmp настроен, но пакет pysnmp не установлен. "
            "Выполните: pip install pysnmp (см. docs/PRINTER_MONITORING_FORECASTING.md)."
        ) from exc

    if isinstance(security, SnmpV2cSecurity):
        security_params = CommunityData(security.community, mpModel=1)  # mpModel=1 -> SNMPv2c
    else:
        auth_protocol_map = {
            None: usmNoAuthProtocol, "MD5": usmHMACMD5AuthProtocol, "SHA": usmHMACSHAAuthProtocol,
            "SHA224": usmHMAC128SHA224AuthProtocol, "SHA256": usmHMAC192SHA256AuthProtocol,
            "SHA384": usmHMAC256SHA384AuthProtocol, "SHA512": usmHMAC384SHA512AuthProtocol,
        }
        priv_protocol_map = {
            None: usmNoPrivProtocol, "DES": usmDESPrivProtocol, "3DES": usm3DESEDEPrivProtocol,
            "AES": usmAesCfb128Protocol, "AES192": usmAesCfb192Protocol, "AES256": usmAesCfb256Protocol,
        }
        security_params = UsmUserData(
            security.username,
            authKey=security.auth_key, privKey=security.priv_key,
            authProtocol=auth_protocol_map[security.auth_protocol],
            privProtocol=priv_protocol_map[security.priv_protocol],
        )

    iterator = getCmd(
        SnmpEngine(),
        security_params,
        UdpTransportTarget((host, port), timeout=timeout, retries=retries),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    error_indication, error_status, _error_index, var_binds = next(iterator)
    if error_indication or error_status:
        # НИКОГДА не включаем security_params/security в это сообщение.
        raise SnmpPollError(f"SNMP GET {oid}@{host} не удался: {error_indication or error_status}")
    return str(var_binds[0][1])


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
    device, profile=None, security: Optional[SnmpSecurity] = None, getter: Optional[Callable] = None,
) -> NormalizedDeviceReading:
    """device — printaudit.models.PrinterDevice (нужен ip_address); profile —
    printaudit.models.SnmpProfile. `getter(host, port, security, oid,
    timeout, retries) -> str|None` — низкоуровневая функция; по умолчанию
    реальный SNMP через pysnmp, в тестах — фейк без сети/железа. Один
    недоступный/неподдерживаемый OID НЕ должен провалить весь опрос —
    соответствующее поле просто останется None (не 0/False).

    Если профиль настроен неполно/некорректно (см. resolve_snmp_security),
    `SnmpConfigError` поднимается НАРУЖУ из poll_device целиком (не
    перехватывается здесь) — опрос ЭТОГО устройства завершается понятной
    ошибкой; вызывающий код (collector/monitor_printers.py) уже оборачивает
    опрос каждого устройства в try/except, поэтому один неверно
    настроенный профиль не проваливает весь прогон по площадке."""
    getter = getter or _default_snmp_get
    now = datetime.now(timezone.utc)

    if not getattr(device, "ip_address", None):
        return NormalizedDeviceReading(
            collected_at=now, source=MONITORING_SOURCE_SNMP, is_reachable=None, device_status="unknown",
            raw_status_text="У устройства не задан IP-адрес",
        )

    resolved_security = security if security is not None else resolve_snmp_security(profile)

    oid_map = _parse_oid_map(profile)
    port = profile.port if profile else 161
    timeout = profile.timeout_seconds if profile else 2.0
    retries = profile.retries if profile else 1

    def _get(field_name: str) -> Optional[str]:
        oid = oid_map.get(field_name)
        if not oid:
            return None
        try:
            return getter(device.ip_address, port, resolved_security, oid, timeout, retries)
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
