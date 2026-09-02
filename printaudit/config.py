"""Загрузка конфигурации проекта из config/config.yaml.

Путь к файлу можно переопределить переменной окружения PRINTAUDIT_CONFIG —
это удобно, если на объекте несколько инстансов или конфиг лежит в другом месте.
"""
import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    env = os.environ.get("PRINTAUDIT_CONFIG")
    return Path(env) if env else REPO_ROOT / "config" / "config.yaml"


class Settings:
    def __init__(self, data: dict, root: Path):
        self.site_code = str(data.get("site_code", "SITE1"))
        self.db_url = data["db"]["url"]
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
