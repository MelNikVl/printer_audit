"""Fail-closed проверка SESSION_SECRET_KEY: веб-приложение должно ОТКАЗАТЬСЯ
запускаться (не просто залогировать предупреждение) без настоящего секрета
сессий, но collector и CLI-скрипты, которым веб-сессии не нужны, не должны
из-за этого ломаться."""
import json

import pytest


def _start_app(monkeypatch, secret):
    """Импортирует webapp.main свежим (после app_env уже сбросил sys.modules)
    и пытается его "поднять" через TestClient — это выполняет lifespan."""
    if secret is None:
        monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SESSION_SECRET_KEY", secret)

    from fastapi.testclient import TestClient

    import webapp.main as main

    with TestClient(main.app):
        pass


def test_app_refuses_to_start_without_session_secret(app_env, monkeypatch):
    from printaudit.ad_settings import InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError, match="не задан"):
        _start_app(monkeypatch, None)


def test_app_refuses_to_start_with_empty_session_secret(app_env, monkeypatch):
    from printaudit.ad_settings import InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError):
        _start_app(monkeypatch, "")


def test_app_refuses_to_start_with_changeme_placeholder(app_env, monkeypatch):
    from printaudit.ad_settings import InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError, match="CHANGE_ME"):
        _start_app(monkeypatch, "CHANGE_ME_GENERATE_A_RANDOM_SECRET")


def test_app_refuses_to_start_with_lowercase_changeme_placeholder(app_env, monkeypatch):
    """Плейсхолдер ищем регистронезависимо -- 'change_me' тоже должен считаться."""
    from printaudit.ad_settings import InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError, match="CHANGE_ME"):
        _start_app(monkeypatch, "change_me_please_replace_this_value_now")


def test_app_refuses_to_start_with_dev_fallback_secret(app_env, monkeypatch):
    from printaudit.ad_settings import DEV_INSECURE_SESSION_SECRET, InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError, match="dev-значением"):
        _start_app(monkeypatch, DEV_INSECURE_SESSION_SECRET)


def test_app_refuses_to_start_with_too_short_secret(app_env, monkeypatch):
    from printaudit.ad_settings import InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError, match="короткий"):
        _start_app(monkeypatch, "short-secret-12345")  # < 32 символов


def test_app_starts_fine_with_a_valid_secret(app_env, monkeypatch):
    import secrets

    _start_app(monkeypatch, secrets.token_urlsafe(48))  # не должно бросить


def test_exactly_min_length_boundary(app_env, monkeypatch):
    """32 символа -- минимум допустимой длины (граничное условие)."""
    from printaudit.ad_settings import MIN_SESSION_SECRET_LENGTH

    exactly_min = "a" * MIN_SESSION_SECRET_LENGTH
    _start_app(monkeypatch, exactly_min)  # ровно минимум -- должно пройти

    from printaudit.ad_settings import InsecureSessionSecretError

    with pytest.raises(InsecureSessionSecretError):
        _start_app(monkeypatch, "a" * (MIN_SESSION_SECRET_LENGTH - 1))


# ---------------------------------------------------------------------------
# Collector и CLI не должны требовать SESSION_SECRET_KEY вообще
# ---------------------------------------------------------------------------


def test_collector_run_once_works_without_session_secret(app_env, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    import collector.collect_print_events as cpe

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr(cpe.subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
    cpe.run_once()  # не должно бросить InsecureSessionSecretError или что-либо ещё


def test_bootstrap_cli_works_without_session_secret(app_env, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    import scripts.bootstrap_superadmin as bootstrap

    monkeypatch.setattr(
        bootstrap.sys, "argv", ["bootstrap_superadmin.py", "--login", "DOMAIN\\ivanov", "--skip-ad-check"]
    )
    rc = bootstrap.main()
    assert rc == 0
