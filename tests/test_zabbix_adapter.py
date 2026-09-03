"""Тесты printaudit.monitoring.zabbix_adapter: нормализация latest values +
active problems в NormalizedDeviceReading, без реального Zabbix (фейковый
транспорт)."""
from printaudit.monitoring.zabbix_adapter import ZabbixApiError, ZabbixClient, poll_device


def _fake_transport(items_by_host=None, problems_by_host=None, raise_on=None):
    items_by_host = items_by_host or {}
    problems_by_host = problems_by_host or {}
    raise_on = raise_on or set()

    def _transport(method, params):
        if method in raise_on:
            raise ZabbixApiError(f"{method} failed")
        host_id = params.get("hostids", [None])[0]
        if method == "item.get":
            return items_by_host.get(host_id, [])
        if method == "problem.get":
            return problems_by_host.get(host_id, [])
        return []

    return _transport


def test_poll_device_normalizes_counters_and_supplies():
    items = {
        "10": [
            {"key_": "printer.pages.total", "lastvalue": "5000"},
            {"key_": "printer.pages.color", "lastvalue": "1200"},
            {"key_": "printer.supply.toner.black", "lastvalue": "45"},
        ]
    }
    client = ZabbixClient("https://zabbix.example", "tok", transport=_fake_transport(items))
    reading = poll_device(client, "10")

    assert reading.source == "zabbix_api"
    assert reading.is_reachable is True
    assert reading.total_pages == 5000
    assert reading.color_pages == 1200
    assert reading.bw_pages is None  # итем не пришёл -- None, не 0
    toner = next(s for s in reading.supplies if s.supply_type == "toner_black")
    assert toner.level_percent == 45
    assert toner.level_status == "ok"


def test_poll_device_missing_item_is_none_not_zero():
    items = {"10": [{"key_": "printer.pages.total", "lastvalue": "5000"}]}
    client = ZabbixClient("https://zabbix.example", "tok", transport=_fake_transport(items))
    reading = poll_device(client, "10")

    toner = [s for s in reading.supplies if s.supply_type == "toner_black"]
    assert toner[0].level_percent is None
    assert toner[0].level_status == "unknown"


def test_poll_device_no_items_returns_unknown_not_reachable():
    client = ZabbixClient("https://zabbix.example", "tok", transport=_fake_transport({}))
    reading = poll_device(client, "999")

    assert reading.is_reachable is None
    assert reading.device_status == "unknown"


def test_poll_device_api_error_on_items_is_handled_gracefully():
    client = ZabbixClient(
        "https://zabbix.example", "tok",
        transport=_fake_transport(raise_on={"item.get"}),
    )
    reading = poll_device(client, "10")

    assert reading.is_reachable is None
    assert reading.device_status == "unknown"
    assert "недоступен" in reading.raw_status_text


def test_poll_device_reports_active_problems_as_alerts():
    items = {"10": [{"key_": "printer.pages.total", "lastvalue": "1"}]}
    problems = {
        "10": [{"eventid": "5001", "name": "Paper Jam", "severity": "4"}],
    }
    client = ZabbixClient("https://zabbix.example", "tok", transport=_fake_transport(items, problems))
    reading = poll_device(client, "10")

    assert reading.device_status == "error"
    assert len(reading.alerts) == 1
    assert reading.alerts[0].external_id == "5001"
    assert reading.alerts[0].severity == "critical"


def test_poll_device_problems_error_does_not_fail_whole_poll():
    items = {"10": [{"key_": "printer.pages.total", "lastvalue": "1"}]}
    client = ZabbixClient(
        "https://zabbix.example", "tok",
        transport=_fake_transport(items, raise_on={"problem.get"}),
    )
    reading = poll_device(client, "10")

    assert reading.is_reachable is True
    assert reading.alerts == []


def test_token_never_appears_in_client_repr_or_error():
    client = ZabbixClient("https://zabbix.example", "super-secret-token", transport=_fake_transport({}))
    reading = poll_device(client, "1")
    assert "super-secret-token" not in repr(reading)
    assert "super-secret-token" not in str(reading.raw_status_text)
