"""Общая инфраструктура тестов.

`printaudit.config.get_settings()` падает с FileNotFoundError, если не найден
config/config.yaml (и переменная окружения PRINTAUDIT_CONFIG не задана) — это
осознанное поведение для продакшена (лучше упасть явно, чем тихо работать без
конфига). Но это же означает, что просто `import collector.collect_print_events`
на этапе сбора тестов (до того, как успеет отработать любая fixture) может
упасть, если в рабочей копии нет config/config.yaml (например, после того как
этот файл уберут из git). Поэтому уже на уровне модуля conftest.py (который
pytest импортирует раньше файлов с тестами) гарантируем существование
временного дефолтного конфига и переменной окружения PRINTAUDIT_CONFIG —
конкретные тесты, которым нужна изолированная БД, дополнительно используют
fixture `app_env`, которая создаёт свой config.yaml на каждый тест.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="printaudit-tests-"))

if not os.environ.get("PRINTAUDIT_CONFIG"):
    _default_cfg = _SESSION_TMP / "default_config.yaml"
    _default_db = _SESSION_TMP / "default.db"
    _default_cfg.write_text(
        f"""
site_code: "TEST-DEFAULT"
db:
  url: "sqlite:///{_default_db.as_posix()}"
paths:
  users_departments_csv: "{(_SESSION_TMP / 'users_departments.csv').as_posix()}"
  log_dir: "{(_SESSION_TMP / 'logs').as_posix()}"
collector:
  log_name: "Microsoft-Windows-PrintService/Operational"
  event_id: 307
  poll_interval_minutes: 2
  max_events_per_run: 5000
  field_map:
    job_id: 0
    document_name: 1
    user_name: 2
    printer_name: 4
    total_pages: 8
currency: "KZT"
default_price_per_page_bw: 8
default_price_per_page_color: 40
""",
        encoding="utf-8",
    )
    (_SESSION_TMP / "users_departments.csv").write_text(
        "user_name,department_name,cost_center_code\n", encoding="utf-8"
    )
    os.environ["PRINTAUDIT_CONFIG"] = str(_default_cfg)


_APP_MODULE_PREFIXES = ("printaudit", "collector", "webapp", "scripts")


def _reset_app_modules():
    for name in list(sys.modules):
        if name.split(".")[0] in _APP_MODULE_PREFIXES:
            sys.modules.pop(name, None)


def _write_test_config(tmp_path, db_path, site_code="TEST"):
    csv_path = tmp_path / "users_departments.csv"
    csv_path.write_text("user_name,department_name,cost_center_code\n", encoding="utf-8")

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""
site_code: "{site_code}"
db:
  url: "sqlite:///{db_path.as_posix()}"
paths:
  users_departments_csv: "{csv_path.as_posix()}"
  log_dir: "{(tmp_path / 'logs').as_posix()}"
collector:
  log_name: "Microsoft-Windows-PrintService/Operational"
  event_id: 307
  poll_interval_minutes: 2
  max_events_per_run: 5000
  field_map:
    job_id: 0
    document_name: 1
    user_name: 2
    printer_name: 4
    total_pages: 8
currency: "KZT"
default_price_per_page_bw: 8
default_price_per_page_color: 40
""",
        encoding="utf-8",
    )
    return cfg_path


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Изолированная конфигурация + пустая SQLite БД (со ВСЕМИ таблицами,
    созданными напрямую через Base.metadata.create_all, без прогона через
    Alembic) на каждый тест.

    Возвращает свежесобранный модуль `printaudit.database` (с engine,
    указывающим на временную БД этого теста) — импортировать
    `printaudit.*`/`collector.*` внутри теста нужно ПОСЛЕ вызова этой fixture,
    иначе получите модули, привязанные к чужой (например, дефолтной сессионной)
    БД из кэша sys.modules.

    Для тестов, которые проверяют сами миграции (в т.ч. миграцию уже
    существующей БД), используйте `db_env` — она не создаёт таблицы заранее.
    """
    db_path = tmp_path / "test.db"
    cfg_path = _write_test_config(tmp_path, db_path)
    monkeypatch.setenv("PRINTAUDIT_CONFIG", str(cfg_path))
    _reset_app_modules()

    import printaudit.database as database
    import printaudit.models  # noqa: F401  -- регистрирует таблицы в database.Base.metadata

    database.Base.metadata.create_all(database.engine)
    yield database
    database.engine.dispose()
    _reset_app_modules()


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Как `app_env`, но БД остаётся полностью пустой (ни одной таблицы) —
    для тестов, которые сами прогоняют Alembic (или сами создают "легаси"
    схему руками перед тем, как прогнать миграции на неё)."""
    db_path = tmp_path / "test.db"
    cfg_path = _write_test_config(tmp_path, db_path)
    monkeypatch.setenv("PRINTAUDIT_CONFIG", str(cfg_path))
    _reset_app_modules()

    import printaudit.database as database

    yield database, db_path
    database.engine.dispose()
    _reset_app_modules()
