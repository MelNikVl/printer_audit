"""Тесты scripts/agent_diagnose.py: должен явно и по шагам сообщать, что не
так с конфигурацией агента, без падения и без утечки AGENT_TOKEN в вывод."""
import importlib


def _reload_diagnose():
    import scripts.agent_diagnose as diagnose

    return importlib.reload(diagnose)


def test_diagnose_fails_fast_when_not_agent_mode(app_env, monkeypatch, capsys):
    monkeypatch.delenv("APP_MODE", raising=False)
    diagnose = _reload_diagnose()

    rc = diagnose.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "APP_MODE=agent" in out
    assert "[FAIL]" in out


def test_diagnose_reports_missing_variables(app_env, monkeypatch, capsys):
    monkeypatch.setenv("APP_MODE", "agent")
    monkeypatch.delenv("CENTRAL_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_SITE_UUID", raising=False)
    monkeypatch.delenv("AGENT_PRINT_SERVER_UUID", raising=False)
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    diagnose = _reload_diagnose()

    rc = diagnose.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "CENTRAL_BASE_URL" in out
    assert "AGENT_TOKEN" in out


def test_diagnose_never_prints_the_token(app_env, monkeypatch, capsys):
    monkeypatch.setenv("APP_MODE", "agent")
    monkeypatch.setenv("CENTRAL_BASE_URL", "https://central.invalid.example")
    monkeypatch.setenv("AGENT_SITE_UUID", "site-1")
    monkeypatch.setenv("AGENT_PRINT_SERVER_UUID", "server-1")
    monkeypatch.setenv("AGENT_TOKEN", "super-secret-token-value")
    diagnose = _reload_diagnose()

    diagnose.main()
    out = capsys.readouterr().out
    assert "super-secret-token-value" not in out


def test_diagnose_all_checks_pass_when_everything_mocked_ok(app_env, monkeypatch, capsys):
    monkeypatch.setenv("APP_MODE", "agent")
    monkeypatch.setenv("CENTRAL_BASE_URL", "https://central.example.local")
    monkeypatch.setenv("AGENT_SITE_UUID", "site-1")
    monkeypatch.setenv("AGENT_PRINT_SERVER_UUID", "server-1")
    monkeypatch.setenv("AGENT_TOKEN", "test-token")
    diagnose = _reload_diagnose()

    monkeypatch.setattr(diagnose.socket, "getaddrinfo", lambda *a, **k: [("dummy",)])

    class _FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(diagnose.socket, "create_connection", lambda *a, **k: _FakeSocket())

    class _FakeSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            return _FakeSocket()

    monkeypatch.setattr(diagnose.ssl, "create_default_context", lambda: _FakeSSLContext())

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "server_status": "online"}

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            assert "super-secret" not in str(kwargs.get("headers", {}))
            return _FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda: _FakeClient())

    rc = diagnose.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("[OK]") >= 5
    assert "[FAIL]" not in out
