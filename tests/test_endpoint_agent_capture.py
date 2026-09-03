"""endpoint_agent.capture — разбор событий/снимка портов через
инжектируемый `runner` (без реального PowerShell/Windows), field_map
калибровка, защита от неоткалиброванных индексов (FieldMapError)."""
import json

import pytest

from endpoint_agent.capture import (
    CaptureError,
    FieldMapError,
    fetch_new_events,
    fetch_port_map,
    parse_raw_event,
)
from endpoint_agent.config import DEFAULT_FIELD_MAP, EndpointAgentConfig
from endpoint_agent.ports import PortInfo


def _cfg(**overrides):
    base = dict(server_base_url="https://x", token="t", endpoint_uuid="u")
    base.update(overrides)
    return EndpointAgentConfig(**base)


def _raw_event(record_id=1, props=None, time_created="2026-09-04T09:00:00.000Z"):
    return {"RecordId": record_id, "TimeCreated": time_created, "Properties": props or []}


def test_fetch_new_events_parses_json_array_from_runner():
    payload = json.dumps([_raw_event(1), _raw_event(2)])
    events = fetch_new_events(_cfg(), after_record_id=0, runner=lambda args: payload)
    assert len(events) == 2


def test_fetch_new_events_empty_output_is_empty_list():
    assert fetch_new_events(_cfg(), 0, runner=lambda args: "") == []


def test_fetch_new_events_single_object_normalized_to_list():
    payload = json.dumps(_raw_event(1))
    events = fetch_new_events(_cfg(), 0, runner=lambda args: payload)
    assert len(events) == 1


def test_fetch_new_events_invalid_json_raises_capture_error():
    with pytest.raises(CaptureError):
        fetch_new_events(_cfg(), 0, runner=lambda args: "not json")


def test_parse_raw_event_extracts_fields_by_field_map():
    props = [None] * 9
    props[DEFAULT_FIELD_MAP["job_id"]] = "42"
    props[DEFAULT_FIELD_MAP["document_name"]] = "report.docx"
    props[DEFAULT_FIELD_MAP["user_name"]] = "DOMAIN\\ivanov"
    props[DEFAULT_FIELD_MAP["source_computer"]] = "PC-01"
    props[DEFAULT_FIELD_MAP["printer_name"]] = "HP-LaserJet"
    props[DEFAULT_FIELD_MAP["total_pages"]] = "3"

    evt = _raw_event(record_id=100, props=props)
    parsed = parse_raw_event(evt, DEFAULT_FIELD_MAP)

    assert parsed.record_id == 100
    assert parsed.job_id == "42"
    assert parsed.document_name == "report.docx"
    assert parsed.user_name == "DOMAIN\\ivanov"
    assert parsed.printer_name == "HP-LaserJet"
    assert parsed.total_pages == 3


def test_parse_raw_event_out_of_range_index_raises_field_map_error():
    evt = _raw_event(record_id=1, props=["only-one"])
    with pytest.raises(FieldMapError):
        parse_raw_event(evt, {"user_name": 0, "printer_name": 5})


def test_parse_raw_event_missing_user_or_printer_raises():
    props = [""] * 9
    evt = _raw_event(record_id=1, props=props)
    with pytest.raises(CaptureError):
        parse_raw_event(evt, DEFAULT_FIELD_MAP)


def test_fetch_port_map_builds_dict_by_printer_name():
    payload = json.dumps(
        [
            {"Name": "HP-LaserJet", "PortName": "USB001", "Type": "Local"},
            {"Name": "OfficePrinter", "PortName": "OfficePrinter", "Type": "Connection"},
        ]
    )
    port_map = fetch_port_map(runner=lambda args: payload)
    assert port_map["HP-LaserJet"] == PortInfo("HP-LaserJet", "USB001", "Local")
    assert port_map["OfficePrinter"].port_type == "Connection"


def test_fetch_port_map_empty_output_is_empty_dict():
    assert fetch_port_map(runner=lambda args: "[]") == {}
