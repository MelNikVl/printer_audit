from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from printaudit.config import REPO_ROOT, get_settings


def _resolve_sqlite_url(url: str) -> str:
    """Делает относительный путь sqlite:///./data/x.db абсолютным от корня репозитория,
    чтобы БД находилась в одном месте независимо от того, откуда запущен процесс
    (Task Scheduler, консоль, служба)."""
    prefix = "sqlite:///./"
    if url.startswith(prefix):
        rel = url[len(prefix):]
        abs_path = (REPO_ROOT / rel).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{abs_path.as_posix()}"
    return url


settings = get_settings()
_engine_url = _resolve_sqlite_url(settings.db_url)
_connect_args = {"check_same_thread": False} if _engine_url.startswith("sqlite") else {}

engine = create_engine(_engine_url, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


if engine.url.get_backend_name() == "sqlite":
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        """SQLite не проверяет внешние ключи, пока это явно не включено на
        КАЖДОМ соединении (это не настройка файла БД). Без этого CASCADE-less
        FK-констрейнты в моделях (departments/app_users/printer_queues/...)
        существуют только "на бумаге" и ничего не проверяют."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
