"""Настройки Active Directory и веб-сессий — ТОЛЬКО из переменных окружения
(опционально подхваченных из локального `.env`, который не коммитится).

Сознательно не смешиваем это с config.yaml: config.yaml лежит в конфиге проекта
и исторически мог считаться безопасным для показа коллеге по диагонали, тогда
как здесь — учётные данные сервисного bind-аккаунта AD и секрет сессий. Даже
факт "какой у нас AD_BASE_DN" мы не хотим держать в файле, который легко по
ошибке закоммитить вместе с кодом.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv

    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)
except ImportError:  # pragma: no cover - python-dotenv не установлен
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


@dataclass
class ADSettings:
    server: str
    port: int
    use_ssl: bool
    domain: str
    base_dn: str
    user_search_base: str
    group_search_base: str
    bind_user: Optional[str]
    bind_password: Optional[str]

    @property
    def is_configured(self) -> bool:
        return bool(self.server and self.base_dn)


DEV_INSECURE_SESSION_SECRET = "INSECURE-DEV-ONLY-SECRET-DO-NOT-USE-IN-PRODUCTION"
MIN_SESSION_SECRET_LENGTH = 32


class InsecureSessionSecretError(RuntimeError):
    """SESSION_SECRET_KEY отсутствует или явно небезопасен. Поднимается ТОЛЬКО
    веб-приложением на старте (см. webapp/main.py -- lifespan) -- не
    вызывается из get_session_settings()/create_session()/и т.п., чтобы
    collector и CLI-скрипты (bootstrap_superadmin.py и другие), которым
    веб-сессии не нужны вообще, не падали из-за отсутствия этой переменной."""


def validate_session_secret(raw_secret: Optional[str]) -> None:
    """Проверяет СЫРОЕ значение SESSION_SECRET_KEY из окружения (не то, что
    вернул get_session_settings() -- там уже подставлена dev-заглушка, и
    разделить "не задан" и "явно указан плейсхолдер" было бы нельзя)."""
    if not raw_secret:
        raise InsecureSessionSecretError(
            "SESSION_SECRET_KEY не задан. Сгенерируйте случайный секрет:\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            "и добавьте в .env (см. .env.example)."
        )
    if "CHANGE_ME" in raw_secret.upper():
        raise InsecureSessionSecretError(
            "SESSION_SECRET_KEY похож на плейсхолдер из .env.example (содержит 'CHANGE_ME'). "
            "Сгенерируйте реальный секрет и замените значение в .env."
        )
    if raw_secret == DEV_INSECURE_SESSION_SECRET:
        raise InsecureSessionSecretError(
            "SESSION_SECRET_KEY совпадает со встроенным dev-значением по умолчанию — "
            "это НЕ секрет, а публично известная строка из исходного кода. Задайте "
            "свой уникальный секрет в .env."
        )
    if len(raw_secret) < MIN_SESSION_SECRET_LENGTH:
        raise InsecureSessionSecretError(
            f"SESSION_SECRET_KEY слишком короткий ({len(raw_secret)} символов, "
            f"минимум {MIN_SESSION_SECRET_LENGTH}). Сгенерируйте:\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )


@dataclass
class SessionSettings:
    secret_key: str
    lifetime_minutes: int
    cookie_secure: bool


def get_ad_settings() -> ADSettings:
    return ADSettings(
        server=os.environ.get("AD_SERVER", ""),
        port=_env_int("AD_PORT", 636),
        use_ssl=_env_bool("AD_USE_SSL", True),
        domain=os.environ.get("AD_DOMAIN", ""),
        base_dn=os.environ.get("AD_BASE_DN", ""),
        user_search_base=os.environ.get("AD_USER_SEARCH_BASE") or os.environ.get("AD_BASE_DN", ""),
        group_search_base=os.environ.get("AD_GROUP_SEARCH_BASE") or os.environ.get("AD_BASE_DN", ""),
        bind_user=os.environ.get("AD_BIND_USER") or None,
        bind_password=os.environ.get("AD_BIND_PASSWORD") or None,
    )


def get_session_settings() -> SessionSettings:
    secret = os.environ.get("SESSION_SECRET_KEY", "")
    if not secret:
        # Не поднимаем сервис без секрета сессий в проде, но не мешаем читать
        # settings.py в контексте, где сессии не используются (например,
        # collector, bootstrap_superadmin.py). Реальная fail-closed проверка —
        # webapp/main.py (lifespan) вызывает validate_session_secret() на
        # СЫРОЕ значение до того, как сюда попадёт эта заглушка.
        secret = DEV_INSECURE_SESSION_SECRET
    return SessionSettings(
        secret_key=secret,
        lifetime_minutes=_env_int("SESSION_LIFETIME_MINUTES", 480),
        cookie_secure=_env_bool("SESSION_COOKIE_SECURE", False),
    )


@dataclass
class AuthAvailability:
    local_enabled: bool
    ad_enabled: bool  # AD_AUTH_ENABLED=true И AD реально настроен (ADSettings.is_configured)


def get_auth_availability() -> AuthAvailability:
    """Единая точка правды "каким провайдером вообще можно входить сейчас".

    LOCAL_AUTH_ENABLED по умолчанию true (иначе можно случайно заблокировать
    себе вход, отключив AD без единого рабочего локального superadmin).
    AD_AUTH_ENABLED тоже по умолчанию true — так апгрейд уже развёрнутого
    сервера, где AD уже настроен и использовался, не отключает его молча;
    но реально AD доступен, только если он ЕЩЁ И настроен (AD_SERVER/
    AD_BASE_DN заданы) — задать один только флаг без остальных переменных
    недостаточно. При ad_enabled=False код НЕ должен обращаться к LDAP вообще
    (см. webapp/auth_routes.py, webapp/admin_routes.py — все места, где
    вызывается ADClient, сначала проверяют этот флаг)."""
    local_enabled = _env_bool("LOCAL_AUTH_ENABLED", True)
    ad_auth_flag = _env_bool("AD_AUTH_ENABLED", True)
    ad_settings = get_ad_settings()
    return AuthAvailability(local_enabled=local_enabled, ad_enabled=ad_auth_flag and ad_settings.is_configured)
