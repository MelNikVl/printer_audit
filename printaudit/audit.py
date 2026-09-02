"""Запись административных действий в audit_log, со скраббингом секретов.

Ни один вызывающий код не должен передавать сюда пароли/секреты напрямую, но
это last-resort защита: если в old_value/new_value случайно попадёт словарь
с ключом, похожим на секрет, значение всё равно будет замаскировано перед
сериализацией в БД."""
import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from printaudit.models import AuditLog
from printaudit.timeutil import utcnow

_SECRET_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "cookie",
    "csrf",
    "bind_password",
)


def _looks_like_secret_key(key: str) -> bool:
    key_lower = key.lower()
    return any(marker in key_lower for marker in _SECRET_KEY_MARKERS)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if _looks_like_secret_key(str(k)) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _serialize(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(_scrub(value), ensure_ascii=False, default=str)


def record(
    session: Session,
    *,
    actor_app_user_id: Optional[int],
    action: str,
    object_type: str,
    object_id: Optional[Any] = None,
    old_value: Any = None,
    new_value: Any = None,
    ip_address: Optional[str] = None,
    session_id: Optional[str] = None,
) -> AuditLog:
    """Добавляет запись в audit_log в текущей сессии SQLAlchemy. Коммит —
    ответственность вызывающего кода (обычно в той же транзакции, что и сама
    административная операция, чтобы запись и действие были атомарны)."""
    entry = AuditLog(
        actor_app_user_id=actor_app_user_id,
        action=action,
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        old_value=_serialize(old_value),
        new_value=_serialize(new_value),
        ip_address=ip_address,
        session_id=session_id,
        created_at=utcnow(),
    )
    session.add(entry)
    return entry
