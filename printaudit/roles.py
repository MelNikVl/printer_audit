"""Роли приложения. Простые строковые константы, не Enum — так их проще
хранить в SQLite/Postgres одинаково и не тащить миграцию enum-типа при
добавлении новой роли в будущем."""

SUPERADMIN = "superadmin"
ADMIN = "admin"
VIEWER = "viewer"

ALL_ROLES = (SUPERADMIN, ADMIN, VIEWER)
ADMIN_ROLES = (SUPERADMIN, ADMIN)  # кому виден раздел /admin

_RANK = {VIEWER: 0, ADMIN: 1, SUPERADMIN: 2}


def at_least(role: str, minimum: str) -> bool:
    """role даёт доступ не ниже minimum (superadmin > admin > viewer)."""
    return _RANK.get(role, -1) >= _RANK.get(minimum, 99)
