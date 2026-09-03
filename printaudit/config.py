"""Загрузка конфигурации проекта из config/config.yaml.

Путь к файлу можно переопределить переменной окружения PRINTAUDIT_CONFIG —
это удобно, если на объекте несколько инстансов или конфиг лежит в другом месте.
"""
import os
import socket
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    env = os.environ.get("PRINTAUDIT_CONFIG")
    return Path(env) if env else REPO_ROOT / "config" / "config.yaml"


class Settings:
    def __init__(self, data: dict, root: Path):
        self.site_code = str(data.get("site_code", "SITE1"))
        # Имя ЭТОГО Windows Print Server — используется для авто-регистрации
        # PrintServer (см. printaudit.sites.get_or_create_local_print_server),
        # который делает print_jobs.print_server_id/printer_queues уникальность
        # корректной даже на площадке с несколькими серверами. По умолчанию —
        # реальное имя компьютера, а не что-то, что нужно придумывать вручную.
        self.server_name = str(data.get("server_name") or socket.gethostname())
        self.db_url = data["db"]["url"]

        agent = data.get("agent", {}) or {}
        # APP_MODE читается из окружения (.env), а не config.yaml — это
        # деплой-параметр, не свойство площадки. См. printaudit.agent_settings.
        self.agent_max_batch_size = int(agent.get("max_batch_size", 500))
        self.agent_http_timeout_seconds = float(agent.get("http_timeout_seconds", 30))
        self.agent_sync_interval_minutes = int(agent.get("sync_interval_minutes", 2))
        self.currency = data.get("currency", "KZT")
        self.default_price_bw = float(data.get("default_price_per_page_bw", 8))
        self.default_price_color = float(data.get("default_price_per_page_color", 40))

        paths = data.get("paths", {}) or {}
        self.users_departments_csv = root / paths.get(
            "users_departments_csv", "config/users_departments.csv"
        )
        self.log_dir = root / paths.get("log_dir", "logs")

        collector = data.get("collector", {}) or {}
        self.log_name = collector.get(
            "log_name", "Microsoft-Windows-PrintService/Operational"
        )
        self.event_id = int(collector.get("event_id", 307))
        self.poll_interval_minutes = int(collector.get("poll_interval_minutes", 2))
        self.max_events_per_run = int(collector.get("max_events_per_run", 5000))
        self.field_map = collector.get("field_map", {}) or {}

        # "full" (по умолчанию, поведение MVP) | "masked" | "none" — см. printaudit/privacy.py
        self.document_name_policy = str(data.get("document_name_policy", "full"))


_settings: "Settings | None" = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        path = _config_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Файл конфигурации не найден: {path}. "
                f"Скопируйте config/config.example.yaml в config/config.yaml и отредактируйте."
            )
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _settings = Settings(data, REPO_ROOT)
    return _settings


def reload_settings() -> Settings:
    """Сбросить кэш настроек (используется в тестах/скриптах при смене конфига)."""
    global _settings
    _settings = None
    return get_settings()
