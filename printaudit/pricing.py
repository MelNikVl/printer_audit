import fnmatch
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from printaudit.models import PriceList


def match_price(session: Session, printer_name: str, settings) -> Tuple[float, Optional[bool], str, str]:
    """Подбирает тариф для задания по имени принтера/очереди печати.

    Правила price_list проверяются в порядке priority (убыв.), затем id.
    Первое совпадение по fnmatch-паттерну побеждает — это ЯВНОЕ решение
    администратора, поэтому is_color здесь всегда определённый bool
    (color_source="queue"). Если ничего не подошло вообще — цвет ДОСТОВЕРНО
    неизвестен (is_color=None, color_source="unknown"), а не "тихо Ч/Б": для
    выставления счёта используется цена Ч/Б по умолчанию из config.yaml как
    консервативная (более дешёвая) оценка, но это решение о ЦЕНЕ, а не
    утверждение, что печать была чёрно-белой — см. docs/MULTISITE_ARCHITECTURE.md,
    раздел про total_pages/цвет, и tests/test_pricing_v2.py.

    Так как Event ID 307 не сообщает, был ли конкретный документ цветным,
    цвет/цена определяются на уровне очереди печати (см. docs/ADMIN_GUIDE.md,
    раздел "Разделение очередей Ч/Б и цвет").
    """
    rows = (
        session.query(PriceList)
        .order_by(PriceList.priority.desc(), PriceList.id.asc())
        .all()
    )
    name = (printer_name or "").lower()
    for row in rows:
        if fnmatch.fnmatch(name, row.printer_name_pattern.lower()):
            return row.price_per_page, bool(row.is_color), row.currency, "queue"
    return settings.default_price_bw, None, settings.currency, "unknown"
