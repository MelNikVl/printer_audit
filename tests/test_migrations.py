"""Тесты миграций Alembic: применение "с нуля" и, что важнее, применение на
уже существующей БД от старого MVP (до Alembic) без потери накопленных
print_jobs. Каждый тест сам открывает БД через sqlite3 напрямую для сборки
"легаси"-схемы, затем прогоняет `alembic upgrade head` через Python API
(без subprocess) и проверяет итог.
"""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


LEGACY_SCHEMA_SQL = """
CREATE TABLE departments (
    id INTEGER PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    cost_center_code VARCHAR(50)
);
CREATE TABLE users (
    user_name VARCHAR(200) PRIMARY KEY,
    department_id INTEGER REFERENCES departments(id),
    is_active BOOLEAN NOT NULL DEFAULT 1
);
CREATE TABLE price_list (
    id INTEGER PRIMARY KEY,
    printer_name_pattern VARCHAR(200) NOT NULL,
    is_color BOOLEAN NOT NULL DEFAULT 0,
    price_per_page FLOAT NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'KZT',
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE print_jobs (
    id INTEGER PRIMARY KEY,
    site_code VARCHAR(50) NOT NULL,
    record_id INTEGER NOT NULL,
    job_id VARCHAR(50),
    time_created DATETIME NOT NULL,
    user_name VARCHAR(200) NOT NULL,
    document_name VARCHAR(500),
    printer_name VARCHAR(200) NOT NULL,
    total_pages INTEGER NOT NULL DEFAULT 0,
    is_color BOOLEAN,
    department_id INTEGER REFERENCES departments(id),
    price_per_page FLOAT,
    cost FLOAT,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_print_jobs_site_record UNIQUE(site_code, record_id)
);
CREATE TABLE collector_state (
    site_code VARCHAR(50) PRIMARY KEY,
    last_record_id INTEGER NOT NULL DEFAULT 0,
    last_run_at DATETIME
);
"""


def _create_legacy_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO departments (id, name, cost_center_code) VALUES (1, 'Buhgalteria', 'CC-100')"
    )
    conn.execute(
        """
        INSERT INTO print_jobs
            (site_code, record_id, job_id, time_created, user_name, document_name,
             printer_name, total_pages, is_color, department_id, price_per_page, cost, created_at)
        VALUES ('LEGACY', 777, '777', '2026-08-01T10:00:00', 'DOMAIN\\ivanov', 'important.pdf',
                'HP-3F-BW', 10, 0, 1, 8, 80, '2026-08-01T10:00:01')
        """
    )
    conn.execute(
        "INSERT INTO collector_state (site_code, last_record_id, last_run_at) "
        "VALUES ('LEGACY', 777, '2026-08-01T10:00:01')"
    )
    conn.commit()
    conn.close()


def _all_tables(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_migrate_fresh_database_creates_full_schema(db_env):
    database, db_path = db_env

    command.upgrade(_alembic_config(), "head")

    tables = _all_tables(db_path)
    expected = {
        "departments", "users", "price_list", "print_jobs", "collector_state",
        "app_users", "web_sessions", "ad_users", "ad_groups", "ad_group_memberships",
        "ad_department_rules", "printer_queues", "price_rules", "audit_log", "sync_runs",
        "alembic_version",
    }
    assert expected <= tables


def test_migrate_fresh_database_matches_orm_models(db_env):
    database, db_path = db_env

    command.upgrade(_alembic_config(), "head")

    from sqlalchemy import inspect

    import printaudit.models  # noqa: F401

    insp = inspect(database.engine)
    db_tables = set(insp.get_table_names())
    model_tables = set(database.Base.metadata.tables.keys())
    assert model_tables - db_tables == set()

    for table in sorted(model_tables):
        model_cols = {c.name for c in database.Base.metadata.tables[table].columns}
        db_cols = {c["name"] for c in insp.get_columns(table)}
        assert model_cols == db_cols, f"{table}: {model_cols} != {db_cols}"


def test_migrate_preexisting_mvp_database_preserves_print_jobs(db_env):
    """Критическое требование: `alembic upgrade head` на БД, созданной старым
    (до этой ветки) `scripts/init_db.py`, не должен требовать удаления БД и не
    должен терять уже накопленные print_jobs/collector_state."""
    database, db_path = db_env
    _create_legacy_db(db_path)

    command.upgrade(_alembic_config(), "head")

    conn = sqlite3.connect(str(db_path))
    try:
        job = conn.execute(
            "SELECT record_id, user_name, document_name, total_pages, cost, department_id "
            "FROM print_jobs WHERE record_id = 777"
        ).fetchone()
        assert job == (777, "DOMAIN\\ivanov", "important.pdf", 10, 80.0, 1)

        dept = conn.execute(
            "SELECT name, cost_center_code, is_active, display_order FROM departments WHERE id = 1"
        ).fetchone()
        assert dept == ("Buhgalteria", "CC-100", 1, 0)  # новые колонки со значениями по умолчанию

        state = conn.execute(
            "SELECT last_record_id FROM collector_state WHERE site_code = 'LEGACY'"
        ).fetchone()
        assert state == (777,)  # курсор коллектора не сброшен миграцией

        tables = _all_tables(db_path)
        for new_table in ("app_users", "printer_queues", "price_rules", "audit_log", "sync_runs"):
            assert new_table in tables
    finally:
        conn.close()


def test_migrate_existing_app_users_backfills_local_auth_columns(db_env):
    """Регрессия для миграции 1eba877fed63: обновление сервера, где уже есть
    app_users (созданные до появления local-провайдера, все через AD), не
    должно терять ни одной строки и должно проставить безопасные значения по
    умолчанию (auth_provider='ad', остальное NULL/0/False) без ручного
    вмешательства."""
    database, db_path = db_env

    # Накатываем ровно ДО новой миграции, создаём app_users вручную (как они
    # выглядели БЕЗ полей local-auth), затем догоняем до head.
    cfg = _alembic_config()
    command.upgrade(cfg, "4980b40c3753")

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO app_users (login_normalized, role, is_active, assigned_at, created_at, updated_at) "
        "VALUES ('domain\\ivanov', 'superadmin', 1, '2026-08-01T10:00:00', '2026-08-01T10:00:00', '2026-08-01T10:00:00')"
    )
    conn.commit()
    conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT login_normalized, role, auth_provider, password_hash, must_change_password, "
            "failed_login_count, locked_until FROM app_users WHERE login_normalized = 'domain\\ivanov'"
        ).fetchone()
        assert row == ("domain\\ivanov", "superadmin", "ad", None, 0, 0, None)
    finally:
        conn.close()


def test_migrate_preexisting_database_is_idempotent(db_env):
    """Повторный `alembic upgrade head` на уже смигрированной БД не должен
    падать (например, из-за попытки создать таблицу, которая уже есть)."""
    database, db_path = db_env
    _create_legacy_db(db_path)

    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # не должно бросить исключение

    job = sqlite3.connect(str(db_path)).execute(
        "SELECT count(*) FROM print_jobs WHERE record_id = 777"
    ).fetchone()
    assert job == (1,)
