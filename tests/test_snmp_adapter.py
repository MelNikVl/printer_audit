"""Тесты printaudit.monitoring.snmp_adapter: нормализация SNMP-показаний
через инъекцию фейкового getter — без реального SNMP/pysnmp/железа."""
from dataclasses import dataclass
from typing import Optional

import pytest

from printaudit.monitoring.snmp_adapter import DEFAULT_OIDS, poll_device


@dataclass
class _FakeDevice:
    id: int = 1
    ip_address: Optional[str] = "10.0.0.5"


@dataclass
class _FakeProfile:
    port: int = 161
    timeout_seconds: float = 2.0
    retries: int = 1
    oid_map_json: str = "{}"
    credentials_env_var: Optional[str] = None
    id: int = 1


def _fake_getter(values: dict, raise_for=None):
    raise_for = raise_for or set()

    def _getter(host, port, community, oid, timeout, retries):
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


def test_resolve_snmp_credential_reads_named_env_var(monkeypatch):
    from printaudit.monitoring.snmp_adapter import resolve_snmp_credential

    monkeypatch.setenv("SNMP_CRED_TEST", "my-community-string")
    profile = _FakeProfile(credentials_env_var="SNMP_CRED_TEST")
    assert resolve_snmp_credential(profile) == "my-community-string"


def test_resolve_snmp_credential_defaults_to_public_when_unset(monkeypatch):
    from printaudit.monitoring.snmp_adapter import resolve_snmp_credential

    monkeypatch.delenv("SNMP_CRED_MISSING", raising=False)
    profile = _FakeProfile(credentials_env_var="SNMP_CRED_MISSING")
    assert resolve_snmp_credential(profile) == "public"


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
        _default_snmp_get("10.0.0.5", 161, "public", "1.2.3", 2.0, 1)
