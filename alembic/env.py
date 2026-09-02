import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Используем ТОТ ЖЕ путь получения URL БД, что и приложение (printaudit.config,
# с учётом PRINTAUDIT_CONFIG) — чтобы `alembic upgrade head` всегда применялся
# к той же базе, с которой работает веб/сборщик, без дублирования конфигурации
# в alembic.ini.
import printaudit.models  # noqa: E402,F401 -- регистрирует все таблицы в Base.metadata
from printaudit.database import Base, _connect_args  # noqa: E402
from printaudit.database import engine as _app_engine  # noqa: E402

# ВАЖНО: миграции выполняются через ОТДЕЛЬНЫЙ engine на том же URL, а не через
# printaudit.database.engine напрямую. Тот engine навешивает
# PRAGMA foreign_keys=ON на каждое соединение (нужно для рабочего режима
# приложения) — но SQLite batch-режим Alembic (render_as_batch, используется
# ниже) пересоздаёт изменяемую таблицу (CREATE new -> COPY -> DROP old ->
# RENAME), и если FK-проверка включена на этом же соединении, DROP падает с
# "FOREIGN KEY constraint failed", как только на таблицу ссылается внешний
# ключ из другой таблицы (например, print_jobs.department_id -> departments).
# Мы пробовали переключать PRAGMA на том же соединении посреди
# context.begin_transaction() — это ломает транзакцию Alembic настолько, что
# миграция молча не коммитится вообще (проверено). Отдельный engine без этого
# listener — самый надёжный вариант: миграции всегда выполняются с выключенной
# проверкой FK (стандартное поведение SQLite по умолчанию), а обычная работа
# приложения — с включённой, через printaudit.database.engine.
_migration_engine = create_engine(str(_app_engine.url), connect_args=_connect_args)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = str(_migration_engine.url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    with _migration_engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    _migration_engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
