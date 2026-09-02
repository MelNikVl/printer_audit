"""Создаёт таблицы БД (если их нет) и засевает price_list значениями по умолчанию
из config.yaml. Запускать один раз при первом развёртывании на объекте:

    python scripts\\init_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.config import get_settings  # noqa: E402
from printaudit.database import Base, SessionLocal, engine  # noqa: E402
from printaudit.models import PriceList  # noqa: E402


def main() -> None:
    Base.metadata.create_all(engine)
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
            print("Отредактируйте price_list под реальные имена очередей печати объекта.")
        print(f"База данных инициализирована: {engine.url}")
        print(f"site_code = {settings.site_code}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
