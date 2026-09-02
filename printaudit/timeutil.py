from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite не хранит offset у DateTime-колонок: значения, записанные как
    aware (datetime.now(timezone.utc)), при чтении из БД возвращаются naive.
    Приводит обе стороны любого сравнения к одному (naive UTC) представлению,
    независимо от того, пришло значение только что созданным объектом или из БД."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
