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
    mode = os.environ.get("APP_MODE", MODE_STANDALONE).strip().lower() or MODE_STANDALONE
    if mode not in VALID_MODES:
        mode = MODE_STANDALONE
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
