import fnmatch

from sqlalchemy.orm import Session

from printaudit.models import PriceList


def match_price(session: Session, printer_name: str, settings) -> tuple[float, bool, str]:
    """Подбирает тариф для задания по имени принтера/очереди печати.

    Правила price_list проверяются в порядке priority (убыв.), затем id.
    Первое совпадение по fnmatch-паттерну побеждает. Если ничего не подошло —
    используется цена Ч/Б по умолчанию из config.yaml.

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
            return row.price_per_page, bool(row.is_color), row.currency
    return settings.default_price_bw, False, settings.currency
