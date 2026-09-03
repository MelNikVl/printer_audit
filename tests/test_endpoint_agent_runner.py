"""endpoint_agent.runner — цикл захвата+отправки на инжектируемых фейках
(event_runner/port_runner/transport), без реального Windows/сети/сервера.
Полная сквозная проверка (реальный webapp через TestClient) — см.
tests/test_endpoint_agent_e2e.py."""
import json
from datetime import datetime, timedelta, timezone

from endpoint_agent import outbox
from endpoint_agent.config import EndpointAgentConfig
from endpoint_agent.runner import capture_cycle, sync_cycle
from endpoint_agent.sync_client import HttpResult, SyncError


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


def _cfg(**overrides):
    base = dict(server_base_url="https://site.local", token="t", endpoint_uuid="u", batch_size=200)
    base.update(overrides)
    return EndpointAgentConfig(**base)


def _event(record_id, printer_name, props_override=None):
    props = [None] * 9
    props[0] = str(record_id)
    props[1] = "doc.docx"
    props[2] = "DOMAIN\\ivanov"
    props[3] = "PC-01"
    props[4] = printer_name
    props[8] = "2"
    if props_override:
        for i, v in props_override.items():
            props[i] = v
    return {"RecordId": record_id, "TimeCreated": "2026-09-04T09:00:00.000Z", "Properties": props}


def _port_map_json(local=("USB-Printer",), network=("NetworkQueue",)):
    items = [{"Name": n, "PortName": "USB001", "Type": "Local"} for n in local]
    items += [{"Name": n, "PortName": n, "Type": "Connection"} for n in network]
    return json.dumps(items)


def test_capture_cycle_excludes_network_queue_and_advances_cursor(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    events_json = json.dumps([_event(1, "USB-Printer"), _event(2, "NetworkQueue")])
    inserted = capture_cycle(
        _cfg(), conn, _Log(),
        event_runner=lambda args: events_json,
        port_runner=lambda args: _port_map_json(),
    )
    assert inserted == 1
    assert outbox.pending_count(conn) == 1
    assert outbox.get_cursor(conn) == 2  # курсор продвинут мимо ОБОИХ событий


def test_capture_cycle_skips_unparseable_event_but_advances_cursor(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    bad_event = _event(1, "USB-Printer", props_override={2: ""})  # пустой user_name
    events_json = json.dumps([bad_event])
    inserted = capture_cycle(
        _cfg(), conn, _Log(),
        event_runner=lambda args: events_json,
        port_runner=lambda args: _port_map_json(),
    )
    assert inserted == 0
    assert outbox.get_cursor(conn) == 1


def test_capture_cycle_noop_when_no_new_events(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    calls = {"port_runner": 0}

    def port_runner(args):
        calls["port_runner"] += 1
        return "[]"

    inserted = capture_cycle(_cfg(), conn, _Log(), event_runner=lambda args: "[]", port_runner=port_runner)
    assert inserted == 0
    assert calls["port_runner"] == 0  # без новых событий снимок портов вообще не запрашивается


def test_sync_cycle_marks_inserted_and_rejected_from_ack_results(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    outbox.enqueue_events(conn, [{"record_id": 1, "printer_name": "A"}, {"record_id": 2, "printer_name": "B"}])

    def transport(url, headers, payload, timeout):
        return HttpResult(
            200,
            {
                "accepted": 1, "duplicates": 0, "rejected": 1,
                "results": [
                    {"record_id": 1, "status": "inserted"},
                    {"record_id": 2, "status": "rejected", "error": "printer_name пуст"},
                ],
            },
        )

    sync_cycle(_cfg(), conn, _Log(), transport=transport)
    assert outbox.pending_count(conn) == 0
    assert outbox.failed_count(conn) == 1


def test_sync_cycle_network_error_leaves_events_pending_with_backoff(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    outbox.enqueue_events(conn, [{"record_id": 1, "printer_name": "A"}])

    def transport(url, headers, payload, timeout):
        raise SyncError("сеть недоступна")

    last_error = sync_cycle(_cfg(), conn, _Log(), transport=transport)
    assert last_error is not None
    assert outbox.pending_count(conn) == 1
    assert outbox.fetch_due_batch(conn, 10) == []  # not due yet (backoff)


def test_sync_cycle_whole_batch_rejected_retries_not_terminal(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    outbox.enqueue_events(conn, [{"record_id": 1, "printer_name": "A"}])

    def transport(url, headers, payload, timeout):
        return HttpResult(400, {"detail": {"error": "unsupported_protocol_version"}})

    sync_cycle(_cfg(), conn, _Log(), transport=transport)
    assert outbox.pending_count(conn) == 1  # НЕ terminal failed -- пакетная ошибка, не поэлементная
    assert outbox.failed_count(conn) == 0


def test_sync_cycle_sends_heartbeat_even_with_empty_queue(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(url)
        return HttpResult(200, {"ok": True})

    sync_cycle(_cfg(), conn, _Log(), transport=transport)
    assert calls == ["https://site.local/api/v1/endpoint/heartbeat"]


def test_sync_cycle_duplicate_ack_treated_as_success(tmp_path):
    conn = outbox.open_db(tmp_path / "o.sqlite3")
    outbox.enqueue_events(conn, [{"record_id": 1, "printer_name": "A"}])

    def transport(url, headers, payload, timeout):
        return HttpResult(200, {"accepted": 0, "duplicates": 1, "rejected": 0, "results": [{"record_id": 1, "status": "duplicate"}]})

    sync_cycle(_cfg(), conn, _Log(), transport=transport)
    assert outbox.pending_count(conn) == 0
    assert outbox.failed_count(conn) == 0
