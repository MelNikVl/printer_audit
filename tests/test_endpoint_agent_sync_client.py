"""endpoint_agent.sync_client — HTTP через инжектируемый transport (без
реальной сети), контракт совпадает с webapp/endpoint_api.py."""
import pytest

from endpoint_agent.sync_client import HttpResult, SyncError, send_events_batch, send_heartbeat


def test_send_events_batch_hits_correct_path_and_auth_header():
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return HttpResult(200, {"accepted": 1, "duplicates": 0, "rejected": 0, "results": []})

    result = send_events_batch("https://site.example.local", "tok123", {"events": []}, 30.0, transport)
    assert captured["url"] == "https://site.example.local/api/v1/endpoint/events/batch"
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert result.status_code == 200


def test_send_heartbeat_hits_correct_path():
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        return HttpResult(200, {"ok": True})

    send_heartbeat("https://site.example.local/", "tok", {}, 30.0, transport)
    assert captured["url"] == "https://site.example.local/api/v1/endpoint/heartbeat"


def test_transport_raising_sync_error_propagates():
    def transport(url, headers, payload, timeout):
        raise SyncError("сеть недоступна")

    with pytest.raises(SyncError):
        send_events_batch("https://site.example.local", "tok", {}, 30.0, transport)


def test_token_never_appears_in_sync_error_message():
    def transport(url, headers, payload, timeout):
        raise SyncError("Сеть недоступна: timeout")

    try:
        send_events_batch("https://site.example.local", "super-secret-token", {}, 30.0, transport)
    except SyncError as exc:
        assert "super-secret-token" not in str(exc)
