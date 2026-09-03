"""Конфигурация endpoint-агента — намеренно свой, минимальный парсер
KEY=VALUE файла (не python-dotenv, не printaudit.config/PyYAML), чтобы
пакет endpoint_agent оставался зависим только от stdlib + pywin32 (см.
endpoint_agent/__init__.py и endpoint_agent/requirements.txt).

Файл endpoint_agent.env создаётся при установке (см.
deploy/install_endpoint_agent.ps1) на основании ENDPOINT_UUID/ENDPOINT_TOKEN,
выданных при регистрации в /admin/endpoint-agents (см. webapp/admin_routes.py)."""
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_EVENT_LOG_NAME = "Microsoft-Windows-PrintService/Operational"
DEFAULT_EVENT_ID = 307
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_MAX_EVENTS_PER_RUN = 2000
DEFAULT_BATCH_SIZE = 200
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0

# Индексы Properties события 307 калибруются по конкретной версии
# Windows/драйвера — см. collector/calibrate_event_fields.ps1, который можно
# запускать и на пользовательском ПК: журнал/событие те же. Значения по
# умолчанию соответствуют типичной раскладке клиентских Windows 10/11 и
# ДОЛЖНЫ быть перепроверены при внедрении (см.
# docs/PRINTER_MONITORING_FORECASTING.md, раздел про endpoint-агента).
DEFAULT_FIELD_MAP: Dict[str, int] = {
    "job_id": 0,
    "document_name": 1,
    "user_name": 2,
    "printer_name": 4,
    "source_computer": 3,
    "total_pages": 8,
}


class ConfigError(ValueError):
    """endpoint_agent.env отсутствует или не содержит обязательных полей —
    агент не должен молча работать без сервера/токена/UUID (в т.ч. чтобы не
    накапливать локальную очередь бесконечно без единого шанса её отправить)."""


@dataclass
class EndpointAgentConfig:
    server_base_url: str
    token: str
    endpoint_uuid: str
    hostname: str = field(default_factory=socket.gethostname)
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    event_log_name: str = DEFAULT_EVENT_LOG_NAME
    event_id: int = DEFAULT_EVENT_ID
    max_events_per_run: int = DEFAULT_MAX_EVENTS_PER_RUN
    batch_size: int = DEFAULT_BATCH_SIZE
    field_map: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_FIELD_MAP))
    printer_allowlist: List[str] = field(default_factory=list)
    printer_denylist: List[str] = field(default_factory=list)
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    db_path: Path = field(default_factory=lambda: Path("endpoint_agent_outbox.sqlite3"))
    log_level: str = "INFO"

    @property
    def agent_version(self) -> str:
        from endpoint_agent import AGENT_VERSION

        return AGENT_VERSION


def _parse_env_lines(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _split_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _field_map_from_env(values: Dict[str, str], base: Dict[str, int]) -> Dict[str, int]:
    result = dict(base)
    prefix = "FIELD_MAP_"
    for key, value in values.items():
        if not key.startswith(prefix):
            continue
        field_name = key[len(prefix):].lower()
        if value == "":
            continue
        try:
            result[field_name] = int(value)
        except ValueError as exc:
            raise ConfigError(f"{key}={value!r} должен быть целым индексом Properties") from exc
    return result


def load_config(path: Path) -> EndpointAgentConfig:
    if not path.exists():
        raise ConfigError(
            f"Файл конфигурации {path} не найден. Создайте его на основе "
            "endpoint_agent/endpoint_agent.env.example и заполните ENDPOINT_UUID/"
            "ENDPOINT_TOKEN, выданные в /admin/endpoint-agents на сервере площадки."
        )
    values = _parse_env_lines(path.read_text(encoding="utf-8"))

    server_base_url = values.get("SERVER_BASE_URL", "").rstrip("/")
    token = values.get("ENDPOINT_TOKEN", "")
    endpoint_uuid = values.get("ENDPOINT_UUID", "")
    missing = [
        name for name, val in (
            ("SERVER_BASE_URL", server_base_url), ("ENDPOINT_TOKEN", token), ("ENDPOINT_UUID", endpoint_uuid),
        ) if not val
    ]
    if missing:
        raise ConfigError(f"В {path} отсутствуют обязательные поля: {', '.join(missing)}")

    base_dir = path.parent
    log_dir = Path(values.get("LOG_DIR") or (base_dir / "logs"))
    db_path = Path(values.get("DB_PATH") or (base_dir / "endpoint_agent_outbox.sqlite3"))

    return EndpointAgentConfig(
        server_base_url=server_base_url,
        token=token,
        endpoint_uuid=endpoint_uuid,
        hostname=values.get("HOSTNAME_OVERRIDE") or socket.gethostname(),
        poll_interval_seconds=int(values.get("POLL_INTERVAL_SECONDS") or DEFAULT_POLL_INTERVAL_SECONDS),
        heartbeat_interval_seconds=int(
            values.get("HEARTBEAT_INTERVAL_SECONDS") or DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        ),
        http_timeout_seconds=float(values.get("HTTP_TIMEOUT_SECONDS") or DEFAULT_HTTP_TIMEOUT_SECONDS),
        event_log_name=values.get("EVENT_LOG_NAME") or DEFAULT_EVENT_LOG_NAME,
        event_id=int(values.get("EVENT_ID") or DEFAULT_EVENT_ID),
        max_events_per_run=int(values.get("MAX_EVENTS_PER_RUN") or DEFAULT_MAX_EVENTS_PER_RUN),
        batch_size=int(values.get("BATCH_SIZE") or DEFAULT_BATCH_SIZE),
        field_map=_field_map_from_env(values, DEFAULT_FIELD_MAP),
        printer_allowlist=_split_list(values.get("PRINTER_ALLOWLIST", "")),
        printer_denylist=_split_list(values.get("PRINTER_DENYLIST", "")),
        log_dir=log_dir,
        db_path=db_path,
        log_level=(values.get("LOG_LEVEL") or "INFO").upper(),
    )
