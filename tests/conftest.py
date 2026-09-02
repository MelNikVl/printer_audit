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


# Валидный (по правилам validate_session_secret) секрет для тестов, которым
# не важна сама эта проверка -- просто чтобы webapp поднимался. Тесты самой
# проверки (test_session_secret_validation.py) НЕ используют эту fixture
# как есть -- они явно управляют SESSION_SECRET_KEY через monkeypatch сами.
TEST_SESSION_SECRET = "test-session-secret-not-for-production-" + "x" * 20


@pytest.fixture
def http_client(app_env, monkeypatch):
    """TestClient для webapp.main.app, привязанный к той же изолированной БД,
    что app_env (webapp.* импортируется свежим ПОСЛЕ app_env, поэтому видит
    правильный printaudit.database). Использовать `login_as()` ниже, чтобы
    получить залогиненную сессию без реального AD."""
    monkeypatch.setenv("SESSION_SECRET_KEY", TEST_SESSION_SECRET)
    from fastapi.testclient import TestClient

    import webapp.main as main

    with TestClient(main.app) as client:
        yield client


def login_as(http_client, role="viewer", login="domain\\testuser", is_active=True):
    """Создаёт AppUser с указанной ролью и заводит ему реальную серверную
    сессию (как после успешного входа), выставляя cookie на http_client —
    без обращения к AD, ровно так, как это делают ролевые/CSRF-тесты, которым
    не нужно перепроверять сам механизм входа (для него есть отдельные тесты
    в tests/test_ad_client.py и tests/test_login_flow.py)."""
    import webapp.main as main  # тот же модуль, что использует http_client
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import SESSION_COOKIE_NAME, create_session

    session = SessionLocal()
    try:
        user = session.query(AppUser).filter_by(login_normalized=login).first()
        if user is None:
            user = AppUser(login_normalized=login, role=role, is_active=is_active)
            session.add(user)
        else:
            user.role = role
            user.is_active = is_active
        session.commit()
        token = create_session(session, user)
        user_id = user.id
    finally:
        session.close()

    http_client.cookies.set(SESSION_COOKIE_NAME, token)
    return user_id
