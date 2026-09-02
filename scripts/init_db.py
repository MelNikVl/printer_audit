"""Применяет миграции Alembic (создаёт БД "с нуля" или обновляет уже
существующую от старого MVP — обе ситуации безопасны, см.
alembic/versions/90fa7d836021_baseline_existing_mvp_schema.py) и засевает
price_list значениями по умолчанию из config.yaml, если он пуст.

Запускать при первом развёртывании на объекте И при каждом обновлении кода,
которое приносит новые миграции:

    python scripts\\init_db.py

Это тонкая обёртка над `alembic upgrade head` + сидинг price_list; сами
миграции можно и нужно применять напрямую через `alembic upgrade head`,
если нужен более тонкий контроль (см. README.md, раздел "Обновление").
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from printaudit.config import REPO_ROOT, get_settings  # noqa: E402
from printaudit.database import SessionLocal, engine  # noqa: E402
from printaudit.models import PriceList  # noqa: E402


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


def main() -> None:
    command.upgrade(_alembic_config(), "head")

    settings = get_settings()
    session = SessionLocal()
    try:
        if session.query(PriceList).count() == 0:
            session.add_all(
                [
                    PriceList(
                        printer_name_pattern="*",
                        is_color=False,
                        price_per_page=settings.default_price_bw,
                        currency=settings.currency,
                        priority=0,
                    ),
                    PriceList(
                        printer_name_pattern="*color*",
                        is_color=True,
                        price_per_page=settings.default_price_color,
                        currency=settings.currency,
                        priority=10,
                    ),
                ]
            )
            session.commit()
            print("Добавлены правила price_list по умолчанию: '*' (Ч/Б) и '*color*' (цвет).")
            print("Отредактируйте price_list (или новые price_rules) под реальные очереди объекта.")
        print(f"База данных обновлена до последней миграции: {engine.url}")
        print(f"site_code = {settings.site_code}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
