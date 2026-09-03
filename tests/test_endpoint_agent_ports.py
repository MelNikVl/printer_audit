"""Классификация локальных/прямых принтеров vs сетевых очередей Print
Server (endpoint_agent.ports) — ключевая защита от задвоения заданий между
endpoint-агентом и Print Server той же площадки."""
from endpoint_agent.ports import (
    REASON_DENYLISTED,
    REASON_NETWORK_QUEUE_EXCLUDED,
    REASON_NOT_ALLOWLISTED,
    REASON_OK,
    REASON_UNKNOWN_PORT,
    PortInfo,
    is_network_port,
    should_capture,
)


def test_local_usb_printer_is_captured():
    port_map = {"HP-LaserJet": PortInfo("HP-LaserJet", "USB001", "Local")}
    keep, reason = should_capture("HP-LaserJet", port_map, [], [])
    assert (keep, reason) == (True, REASON_OK)


def test_network_queue_by_type_is_excluded():
    port_map = {"OfficePrinter": PortInfo("OfficePrinter", "OfficePrinter", "Connection")}
    keep, reason = should_capture("OfficePrinter", port_map, [], [])
    assert (keep, reason) == (False, REASON_NETWORK_QUEUE_EXCLUDED)


def test_network_queue_by_unc_name_fallback_when_type_missing():
    info = PortInfo("OfficePrinter", "\\\\PRINTSRV\\OfficePrinter", None)
    assert is_network_port(info) is True
    port_map = {"OfficePrinter": info}
    keep, reason = should_capture("OfficePrinter", port_map, [], [])
    assert (keep, reason) == (False, REASON_NETWORK_QUEUE_EXCLUDED)


def test_unknown_printer_not_in_port_map_is_excluded_not_double_counted():
    keep, reason = should_capture("SomePrinter", {}, [], [])
    assert (keep, reason) == (False, REASON_UNKNOWN_PORT)


def test_denylist_excludes_even_local_printer():
    port_map = {"Fax": PortInfo("Fax", "SHRFAX:", "Local")}
    keep, reason = should_capture("Fax", port_map, [], ["Fax"])
    assert (keep, reason) == (False, REASON_DENYLISTED)


def test_allowlist_excludes_printers_not_matching():
    port_map = {"HP-LaserJet": PortInfo("HP-LaserJet", "USB001", "Local")}
    keep, reason = should_capture("HP-LaserJet", port_map, ["Canon-*"], [])
    assert (keep, reason) == (False, REASON_NOT_ALLOWLISTED)


def test_allowlist_glob_matches():
    port_map = {"HP-LaserJet-M404": PortInfo("HP-LaserJet-M404", "USB001", "Local")}
    keep, reason = should_capture("HP-LaserJet-M404", port_map, ["HP-*"], [])
    assert (keep, reason) == (True, REASON_OK)


def test_wsd_and_direct_ip_ports_are_local():
    port_map = {
        "WSD-Printer": PortInfo("WSD-Printer", "WSD-a1b2c3", "Local"),
        "Direct-IP-Printer": PortInfo("Direct-IP-Printer", "IP_192.168.1.50", "Local"),
    }
    assert should_capture("WSD-Printer", port_map, [], []) == (True, REASON_OK)
    assert should_capture("Direct-IP-Printer", port_map, [], []) == (True, REASON_OK)
