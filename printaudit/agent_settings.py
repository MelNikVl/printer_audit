"""Настройки режима приложения (standalone/agent/central) — только из
переменных окружения (.env), по тому же принципу, что и printaudit.ad_settings:
это деплой-параметры, а не свойства площадки (config.yaml), и секрет агента
(AGENT_TOKEN) не должен лежать рядом с кодом/config.yaml, который легко
закоммитить по ошибке.
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

MODE_STANDALONE = "standalone"
MODE_AGENT = "agent"
MODE_CENTRAL = "central"
VALID_MODES = (MODE_STANDALONE, MODE_AGENT, MODE_CENTRAL)

PROTOCOL_VERSION = 1
AGENT_VERSION = "1.0"


class InvalidAppModeError(RuntimeError):
    """APP_MODE задан (не пуст), но не входит в VALID_MODES — почти всегда
    опечатка в .env. Раньше это молча превращалось в "standalone", то есть
    опечатка в production-конфиге незаметно меняла режим работы всего
    приложения (например, реально настроенный central тихо переставал
    принимать события от агентов). Теперь — fail fast, как и
    InsecureSessionSecretError в printaudit.ad_settings: приложение/скрипт
    должны явно отказаться работать, а не догадываться, что имел в виду
    администратор."""


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AgentSettings:
    mode: str
    central_base_url: Optional[str]
    site_uuid: Optional[str]
    print_server_uuid: Optional[str]
    token: Optional[str]
    require_https: bool

    @property
    def is_configured(self) -> bool:
        return bool(self.central_base_url and self.site_uuid and self.print_server_uuid and self.token)


def get_agent_settings() -> AgentSettings:
    raw_mode = os.environ.get("APP_MODE")
    if raw_mode is None or not raw_mode.strip():
        # Переменная вообще не задана — это НЕ ошибка, обычный standalone
        # по умолчанию (обратная совместимость с деплоями без .env-строки
        # APP_MODE вообще).
        mode = MODE_STANDALONE
    else:
        mode = raw_mode.strip().lower()
        if mode not in VALID_MODES:
            raise InvalidAppModeError(
                f"APP_MODE={raw_mode!r} — недопустимое значение. "
                f"Допустимые значения: {', '.join(VALID_MODES)}. "
                "Проверьте .env: похоже на опечатку."
            )
    return AgentSettings(
        mode=mode,
        central_base_url=(os.environ.get("CENTRAL_BASE_URL") or None),
        site_uuid=(os.environ.get("AGENT_SITE_UUID") or None),
        print_server_uuid=(os.environ.get("AGENT_PRINT_SERVER_UUID") or None),
        token=(os.environ.get("AGENT_TOKEN") or None),
        require_https=_env_bool("AGENT_REQUIRE_HTTPS", True),
    )


def is_agent_mode() -> bool:
    return get_agent_settings().mode == MODE_AGENT


def is_central_mode() -> bool:
    return get_agent_settings().mode == MODE_CENTRAL
