"""Тесты printaudit.agent_settings: невалидный APP_MODE должен явно
падать (fail-fast), а не молча откатываться на standalone — опечатка в
production-конфигурации не должна незаметно менять режим приложения."""
import pytest


def test_unset_app_mode_defaults_to_standalone(app_env, monkeypatch):
    monkeypatch.delenv("APP_MODE", raising=False)
    from printaudit.agent_settings import MODE_STANDALONE, get_agent_settings

    assert get_agent_settings().mode == MODE_STANDALONE


def test_empty_app_mode_defaults_to_standalone(app_env, monkeypatch):
    monkeypatch.setenv("APP_MODE", "")
    from printaudit.agent_settings import MODE_STANDALONE, get_agent_settings

    assert get_agent_settings().mode == MODE_STANDALONE


@pytest.mark.parametrize("valid_mode", ["standalone", "agent", "central", "AGENT", " Central "])
def test_valid_app_modes_are_accepted_case_and_whitespace_insensitively(app_env, monkeypatch, valid_mode):
    monkeypatch.setenv("APP_MODE", valid_mode)
    from printaudit.agent_settings import get_agent_settings

    assert get_agent_settings().mode == valid_mode.strip().lower()


@pytest.mark.parametrize("bad_mode", ["agnet", "Agent ", "AGENTS", "prod", "central2", "STAND_ALONE"])
def test_invalid_app_mode_raises_instead_of_silently_defaulting(app_env, monkeypatch, bad_mode):
    monkeypatch.setenv("APP_MODE", bad_mode)
    from printaudit.agent_settings import InvalidAppModeError, get_agent_settings

    # "Agent " (с пробелом) должен и сам по себе нормализоваться (strip),
    # поэтому исключаем его отдельно ниже -- здесь только те, что реально
    # не входят в VALID_MODES даже после strip/lower.
    if bad_mode.strip().lower() in ("agent", "standalone", "central"):
        pytest.skip("нормализуется в валидное значение, это не опечатка")
    with pytest.raises(InvalidAppModeError, match=bad_mode.strip()[:4]):
        get_agent_settings()


def test_invalid_app_mode_error_message_names_valid_options(app_env, monkeypatch):
    monkeypatch.setenv("APP_MODE", "centrall")
    from printaudit.agent_settings import InvalidAppModeError, get_agent_settings

    with pytest.raises(InvalidAppModeError) as exc_info:
        get_agent_settings()
    message = str(exc_info.value)
    assert "standalone" in message
    assert "agent" in message
    assert "central" in message


def test_webapp_fails_to_start_with_invalid_app_mode(app_env, monkeypatch):
    """Как и SESSION_SECRET_KEY, APP_MODE проверяется при старте всего
    приложения (lifespan), не только там, где он читается лениво — иначе
    сервер поднимется и будет годами молча работать в непредусмотренном
    режиме, пока кто-то случайно не попадёт на код, который его читает."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret-not-for-production-" + "x" * 20)
    monkeypatch.setenv("APP_MODE", "centrall")

    import webapp.main as main
    from fastapi.testclient import TestClient
    from printaudit.agent_settings import InvalidAppModeError

    with pytest.raises(InvalidAppModeError):
        with TestClient(main.app):
            pass


def test_collector_run_fails_loudly_with_invalid_app_mode(app_env, monkeypatch):
    """collect_print_events.run_once() читает is_agent_mode()/APP_MODE
    каждый прогон -- невалидный режим должен провалить прогон (и попасть в
    sync_runs как failed), а не тихо продолжить как будто ничего не было."""
    import json

    monkeypatch.setenv("APP_MODE", "centrall")
    import collector.collect_print_events as cpe
    from printaudit.agent_settings import InvalidAppModeError

    def _fake_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        class _P:
            returncode = 0
            stdout = "[]"
            stderr = ""

        return _P()

    monkeypatch.setattr(cpe.subprocess, "run", _fake_run)

    with pytest.raises(InvalidAppModeError):
        cpe.run_once()
