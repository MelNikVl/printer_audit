"""Тесты printaudit.monitoring.snmp_adapter: нормализация SNMP-показаний
через инъекцию фейкового getter (без реального SNMP/pysnmp/железа), и
resolve_snmp_security — сборка параметров SNMPv3 USM / явного legacy v2c
без каких-либо неявных fallback на community='public'."""
from dataclasses import dataclass
from typing import Optional

import pytest

from printaudit.monitoring.snmp_adapter import (
    DEFAULT_OIDS,
    SnmpConfigError,
    SnmpV2cSecurity,
    SnmpV3Security,
    poll_device,
    resolve_snmp_security,
)


@dataclass
class _FakeDevice:
    id: int = 1
    ip_address: Optional[str] = "10.0.0.5"


@dataclass
class _FakeProfile:
    """По умолчанию — валидный минимальный SNMPv3 noAuthNoPriv (только
    username, без auth/priv) — ни одна переменная окружения не требуется,
    поэтому тесты нормализации OID не зависят от credential-тестов."""

    name: str = "test-profile"
    id: int = 1
    port: int = 161
    timeout_seconds: float = 2.0
    retries: int = 1
    oid_map_json: str = "{}"
    snmp_version: str = "v3"
    credentials_env_var: Optional[str] = None
    snmp_v3_username: Optional[str] = "printer-monitor"
    snmp_v3_auth_protocol: Optional[str] = None
    snmp_v3_auth_key_env_var: Optional[str] = None
    snmp_v3_priv_protocol: Optional[str] = None
    snmp_v3_priv_key_env_var: Optional[str] = None


def _fake_getter(values: dict, raise_for=None):
    raise_for = raise_for or set()

    def _getter(host, port, security, oid, timeout, retries):
        if oid in raise_for:
            raise RuntimeError("timeout")
        return values.get(oid)

    return _getter


def test_poll_device_online_with_supplies():
    values = {
        DEFAULT_OIDS["device_status"]: "3",  # idle -> online
        DEFAULT_OIDS["total_pages"]: "12345",
        DEFAULT_OIDS["toner_black"]: "60",
    }
    reading = poll_device(_FakeDevice(), _FakeProfile(), getter=_fake_getter(values))

    assert reading.source == "direct_snmp"
    assert reading.is_reachable is True
    assert reading.device_status == "online"
    assert reading.total_pages == 12345
    toner = next(s for s in reading.supplies if s.supply_type == "toner_black")
    assert toner.level_percent == 60


def test_poll_device_no_ip_address_is_unknown():
    reading = poll_device(_FakeDevice(ip_address=None), _FakeProfile(), getter=_fake_getter({}))
    assert reading.is_reachable is None
    assert reading.device_status == "unknown"


def test_poll_device_no_ip_address_does_not_require_valid_security():
    """Отсутствие IP отсекается ДО разбора security -- невалидный/
    отсутствующий профиль не должен мешать вернуть honest "unknown"."""
    reading = poll_device(_FakeDevice(ip_address=None), profile=None, getter=_fake_getter({}))
    assert reading.device_status == "unknown"


def test_poll_device_unreachable_when_status_oid_times_out():
    reading = poll_device(
        _FakeDevice(), _FakeProfile(),
        getter=_fake_getter({}, raise_for={DEFAULT_OIDS["device_status"]}),
    )
    assert reading.is_reachable is False
    assert reading.device_status == "offline"


def test_poll_device_missing_supply_oid_is_none_not_zero():
    values = {DEFAULT_OIDS["device_status"]: "3"}  # toner OID never answers
    reading = poll_device(_FakeDevice(), _FakeProfile(), getter=_fake_getter(values, raise_for={DEFAULT_OIDS["toner_black"]}))

    toner = next(s for s in reading.supplies if s.supply_type == "toner_black")
    assert toner.level_percent is None


def test_poll_device_one_bad_oid_does_not_fail_whole_poll():
    values = {DEFAULT_OIDS["device_status"]: "4", DEFAULT_OIDS["total_pages"]: "999"}
    reading = poll_device(
        _FakeDevice(), _FakeProfile(),
        getter=_fake_getter(values, raise_for={DEFAULT_OIDS["toner_black"]}),
    )
    assert reading.device_status == "online"
    assert reading.total_pages == 999


def test_poll_device_hardware_error_status_produces_alert():
    values = {DEFAULT_OIDS["device_status"]: "1"}  # "other"
    reading = poll_device(_FakeDevice(), _FakeProfile(), getter=_fake_getter(values))

    assert reading.device_status == "warning"
    assert len(reading.alerts) == 1
    assert reading.alerts[0].alert_type == "hardware_error"
    assert reading.alerts[0].external_id == "hardware_error"


def test_oid_map_json_override_extends_default_oids():
    profile = _FakeProfile(oid_map_json='{"toner_cyan": "1.2.3.4.5"}')
    values = {DEFAULT_OIDS["device_status"]: "3", "1.2.3.4.5": "77"}
    reading = poll_device(_FakeDevice(), profile, getter=_fake_getter(values))

    cyan = next(s for s in reading.supplies if s.supply_type == "toner_cyan")
    assert cyan.level_percent == 77


def test_invalid_oid_map_json_falls_back_to_defaults_without_crashing():
    profile = _FakeProfile(oid_map_json="not valid json{{{")
    values = {DEFAULT_OIDS["device_status"]: "3"}
    reading = poll_device(_FakeDevice(), profile, getter=_fake_getter(values))
    assert reading.device_status == "online"


def test_poll_device_config_error_propagates_not_swallowed():
    """Неполная конфигурация SNMP -- ошибка должна завершить опрос ЭТОГО
    устройства понятным исключением (перехватывается на уровне
    collector/monitor_printers.py::_poll_snmp_devices, не здесь)."""
    profile = _FakeProfile(snmp_v3_username=None)
    with pytest.raises(SnmpConfigError, match="snmp_v3_username"):
        poll_device(_FakeDevice(), profile, getter=_fake_getter({}))


def test_poll_device_security_can_be_injected_directly():
    """security= позволяет тестам/вызывающему коду обойти
    resolve_snmp_security целиком (например, когда security уже вычислена
    один раз для нескольких устройств одного профиля)."""
    values = {DEFAULT_OIDS["device_status"]: "3"}
    reading = poll_device(
        _FakeDevice(), profile=None, security=SnmpV2cSecurity(community="injected"),
        getter=_fake_getter(values),
    )
    assert reading.device_status == "online"


# --- resolve_snmp_security -----------------------------------------------


def test_resolve_security_no_profile_raises_config_error():
    with pytest.raises(SnmpConfigError, match="профиль не задан"):
        resolve_snmp_security(None)


def test_resolve_security_unknown_version_raises():
    profile = _FakeProfile(snmp_version="v1")
    with pytest.raises(SnmpConfigError, match="snmp_version"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_no_auth_no_priv():
    profile = _FakeProfile(snmp_v3_username="monitor-user")
    security = resolve_snmp_security(profile)
    assert security == SnmpV3Security(username="monitor-user")


def test_resolve_security_v3_missing_username_raises():
    profile = _FakeProfile(snmp_v3_username=None)
    with pytest.raises(SnmpConfigError, match="snmp_v3_username"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_auth_no_priv_reads_key_from_env(monkeypatch):
    monkeypatch.setenv("SNMP_V3_AUTH_KEY", "super-secret-auth-passphrase")
    profile = _FakeProfile(
        snmp_v3_username="monitor-user", snmp_v3_auth_protocol="sha", snmp_v3_auth_key_env_var="SNMP_V3_AUTH_KEY",
    )
    security = resolve_snmp_security(profile)
    assert security == SnmpV3Security(username="monitor-user", auth_protocol="SHA", auth_key="super-secret-auth-passphrase")


def test_resolve_security_v3_auth_protocol_without_key_env_var_raises():
    profile = _FakeProfile(snmp_v3_username="u", snmp_v3_auth_protocol="SHA", snmp_v3_auth_key_env_var=None)
    with pytest.raises(SnmpConfigError, match="snmp_v3_auth_key_env_var"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_auth_key_env_var_unset_raises(monkeypatch):
    monkeypatch.delenv("SNMP_V3_AUTH_KEY_MISSING", raising=False)
    profile = _FakeProfile(
        snmp_v3_username="u", snmp_v3_auth_protocol="SHA", snmp_v3_auth_key_env_var="SNMP_V3_AUTH_KEY_MISSING",
    )
    with pytest.raises(SnmpConfigError, match="SNMP_V3_AUTH_KEY_MISSING"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_unknown_auth_protocol_raises():
    profile = _FakeProfile(snmp_v3_username="u", snmp_v3_auth_protocol="ROT13")
    with pytest.raises(SnmpConfigError, match="auth_protocol"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_auth_priv_reads_both_keys(monkeypatch):
    monkeypatch.setenv("SNMP_V3_AUTH_KEY2", "auth-secret")
    monkeypatch.setenv("SNMP_V3_PRIV_KEY2", "priv-secret")
    profile = _FakeProfile(
        snmp_v3_username="monitor-user", snmp_v3_auth_protocol="SHA256", snmp_v3_auth_key_env_var="SNMP_V3_AUTH_KEY2",
        snmp_v3_priv_protocol="aes256", snmp_v3_priv_key_env_var="SNMP_V3_PRIV_KEY2",
    )
    security = resolve_snmp_security(profile)
    assert security == SnmpV3Security(
        username="monitor-user", auth_protocol="SHA256", auth_key="auth-secret",
        priv_protocol="AES256", priv_key="priv-secret",
    )


def test_resolve_security_v3_priv_without_auth_raises():
    profile = _FakeProfile(snmp_v3_username="u", snmp_v3_priv_protocol="AES")
    with pytest.raises(SnmpConfigError, match="privacy без authentication|priv_protocol"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_priv_protocol_without_key_env_var_raises(monkeypatch):
    monkeypatch.setenv("SNMP_V3_AUTH_KEY3", "auth-secret")
    profile = _FakeProfile(
        snmp_v3_username="u", snmp_v3_auth_protocol="SHA", snmp_v3_auth_key_env_var="SNMP_V3_AUTH_KEY3",
        snmp_v3_priv_protocol="AES", snmp_v3_priv_key_env_var=None,
    )
    with pytest.raises(SnmpConfigError, match="snmp_v3_priv_key_env_var"):
        resolve_snmp_security(profile)


def test_resolve_security_v3_unknown_priv_protocol_raises(monkeypatch):
    monkeypatch.setenv("SNMP_V3_AUTH_KEY4", "auth-secret")
    profile = _FakeProfile(
        snmp_v3_username="u", snmp_v3_auth_protocol="SHA", snmp_v3_auth_key_env_var="SNMP_V3_AUTH_KEY4",
        snmp_v3_priv_protocol="BLOWFISH",
    )
    with pytest.raises(SnmpConfigError, match="priv_protocol"):
        resolve_snmp_security(profile)


def test_resolve_security_v2c_reads_community_from_env(monkeypatch):
    monkeypatch.setenv("SNMP_CRED_TEST", "my-community-string")
    profile = _FakeProfile(snmp_version="v2c", credentials_env_var="SNMP_CRED_TEST")
    assert resolve_snmp_security(profile) == SnmpV2cSecurity(community="my-community-string")


def test_resolve_security_v2c_missing_env_var_name_raises():
    profile = _FakeProfile(snmp_version="v2c", credentials_env_var=None)
    with pytest.raises(SnmpConfigError, match="credentials_env_var"):
        resolve_snmp_security(profile)


def test_resolve_security_v2c_env_var_unset_raises_no_fallback_to_public(monkeypatch):
    """КЛЮЧЕВОЙ регрессионный тест: раньше отсутствие переменной окружения
    тихо давало community='public'. Теперь -- явная ошибка, ничего не
    опрашивается неявно с публичным community."""
    monkeypatch.delenv("SNMP_CRED_MISSING", raising=False)
    profile = _FakeProfile(snmp_version="v2c", credentials_env_var="SNMP_CRED_MISSING")
    with pytest.raises(SnmpConfigError, match="public") as exc_info:
        resolve_snmp_security(profile)
    assert "SNMP_CRED_MISSING" in str(exc_info.value)


def test_real_snmp_get_requires_pysnmp_with_clear_error(monkeypatch):
    """Без pysnmp установленного (или если импорт заблокирован) поведение
    должно быть понятной ошибкой, а не ImportError вглубь стека."""
    import builtins

    from printaudit.monitoring.snmp_adapter import SnmpPollError, _default_snmp_get

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "pysnmp.hlapi" or name.startswith("pysnmp"):
            raise ImportError("pysnmp not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    with pytest.raises(SnmpPollError, match="pysnmp"):
        _default_snmp_get("10.0.0.5", 161, SnmpV2cSecurity(community="public"), "1.2.3", 2.0, 1)
