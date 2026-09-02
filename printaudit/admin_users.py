"""Управление локальными учётками приложения (app_users) — назначение и
изменение ролей, отключение, удаление назначения. Все "нельзя" из
требований к безопасности живут здесь, а не в HTTP-роутах, чтобы:
  - гарантированно применяться независимо от того, через какой endpoint
    вызвано действие;
  - быть тестируемыми без поднятия FastAPI/HTTP.
"""
import secrets
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from printaudit import audit, roles
from printaudit.ad_normalize import normalize_login
from printaudit.models import AppUser
from printaudit.security.passwords import hash_password
from printaudit.timeutil import utcnow


class AdminActionError(Exception):
    """Ожидаемая ошибка бизнес-правила (показывается пользователю как есть,
    не как внутренняя ошибка 500)."""


def count_active_superadmins(session: Session, exclude_id: Optional[int] = None) -> int:
    q = session.query(AppUser).filter(AppUser.role == roles.SUPERADMIN, AppUser.is_active.is_(True))
    if exclude_id is not None:
        q = q.filter(AppUser.id != exclude_id)
    return q.count()


def _require_known_role(role: str) -> None:
    if role not in roles.ALL_ROLES:
        raise AdminActionError(f"Неизвестная роль: {role}")


def upsert_admin_assignment(
    session: Session,
    *,
    actor: AppUser,
    login_normalized: str,
    role: str,
    ad_sid: Optional[str] = None,
    ad_object_guid: Optional[str] = None,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AppUser:
    """Создаёт новое назначение или обновляет роль существующего.
    Не коммитит — вызывающий код решает, где границы транзакции."""
    _require_known_role(role)

    if actor.role == roles.ADMIN and role == roles.SUPERADMIN:
        raise AdminActionError("Роль admin не может назначать роль superadmin — это может только superadmin.")

    existing = session.query(AppUser).filter_by(login_normalized=login_normalized).first()

    if existing is not None and existing.id == actor.id and existing.role != role:
        raise AdminActionError("Нельзя изменить собственную роль.")

    if (
        existing is not None
        and existing.is_active
        and existing.role == roles.SUPERADMIN
        and role != roles.SUPERADMIN
        and count_active_superadmins(session, exclude_id=existing.id) == 0
    ):
        raise AdminActionError("Нельзя понизить последнего активного superadmin.")

    now = utcnow()

    if existing is None:
        app_user = AppUser(
            login_normalized=login_normalized,
            ad_sid=ad_sid,
            ad_object_guid=ad_object_guid,
            display_name=display_name,
            email=email,
            role=role,
            is_active=True,
            assigned_by_id=actor.id,
            assigned_at=now,
        )
        session.add(app_user)
        session.flush()
        audit.record(
            session,
            actor_app_user_id=actor.id,
            action="admin.create",
            object_type="app_user",
            object_id=app_user.id,
            new_value={"login_normalized": login_normalized, "role": role},
            ip_address=ip_address,
        )
        return app_user

    old_snapshot = {"role": existing.role, "is_active": existing.is_active}
    existing.role = role
    existing.ad_sid = ad_sid or existing.ad_sid
    existing.ad_object_guid = ad_object_guid or existing.ad_object_guid
    existing.display_name = display_name or existing.display_name
    existing.email = email or existing.email
    existing.is_active = True
    existing.disabled_at = None
    existing.disabled_by_id = None
    existing.assigned_by_id = actor.id
    existing.assigned_at = now
    existing.updated_at = now
    audit.record(
        session,
        actor_app_user_id=actor.id,
        action="admin.update_role",
        object_type="app_user",
        object_id=existing.id,
        old_value=old_snapshot,
        new_value={"role": role, "is_active": True},
        ip_address=ip_address,
    )
    return existing


def generate_temporary_password() -> str:
    """~22 случайных URL-safe символа — заведомо длиннее MIN_PASSWORD_LENGTH,
    показывается администратору РОВНО ОДИН РАЗ в flash-сообщении и никогда
    не сохраняется в открытом виде (только Argon2id-хэш в AppUser)."""
    return secrets.token_urlsafe(16)


def create_local_user(
    session: Session,
    *,
    actor: AppUser,
    login: str,
    role: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Tuple[AppUser, str]:
    """Создаёт НОВОГО локального пользователя (auth_provider="local") со
    случайным временным паролем и must_change_password=True. Возвращает
    (AppUser, временный_пароль) — пароль нужно показать актёру ОДИН раз
    (например, во flash-сообщении редиректа) и никогда не логировать/не
    сохранять отдельно от Argon2id-хэша. Не коммитит."""
    _require_known_role(role)
    if actor.role == roles.ADMIN and role == roles.SUPERADMIN:
        raise AdminActionError("Роль admin не может назначать роль superadmin — это может только superadmin.")

    login_normalized = normalize_login(login)
    if session.query(AppUser).filter_by(login_normalized=login_normalized).first():
        raise AdminActionError(f"Пользователь с логином «{login_normalized}» уже существует.")

    temp_password = generate_temporary_password()
    now = utcnow()
    user = AppUser(
        login_normalized=login_normalized,
        display_name=display_name or None,
        email=email or None,
        role=role,
        is_active=True,
        auth_provider="local",
        password_hash=hash_password(temp_password),
        must_change_password=True,
        assigned_by_id=actor.id,
        assigned_at=now,
    )
    session.add(user)
    session.flush()
    audit.record(
        session,
        actor_app_user_id=actor.id,
        action="admin.create_local_user",
        object_type="app_user",
        object_id=user.id,
        new_value={"login_normalized": login_normalized, "role": role},
        ip_address=ip_address,
    )
    return user, temp_password


def disable_admin(session: Session, *, actor: AppUser, target: AppUser, ip_address: Optional[str] = None) -> None:
    if target.id == actor.id:
        raise AdminActionError("Нельзя отключить доступ самому себе.")
    if target.role == roles.SUPERADMIN and target.is_active:
        if count_active_superadmins(session, exclude_id=target.id) == 0:
            raise AdminActionError("Нельзя отключить последнего активного superadmin.")

    old_snapshot = {"is_active": target.is_active}
    target.is_active = False
    target.disabled_at = utcnow()
    target.disabled_by_id = actor.id
    audit.record(
        session,
        actor_app_user_id=actor.id,
        action="admin.disable",
        object_type="app_user",
        object_id=target.id,
        old_value=old_snapshot,
        new_value={"is_active": False},
        ip_address=ip_address,
    )


def delete_admin_assignment(session: Session, *, actor: AppUser, target: AppUser, ip_address: Optional[str] = None) -> None:
    if target.id == actor.id:
        raise AdminActionError("Нельзя удалить собственное назначение.")
    if target.role == roles.SUPERADMIN and target.is_active:
        if count_active_superadmins(session, exclude_id=target.id) == 0:
            raise AdminActionError("Нельзя удалить последнего активного superadmin.")

    audit.record(
        session,
        actor_app_user_id=actor.id,
        action="admin.delete",
        object_type="app_user",
        object_id=target.id,
        old_value={"login_normalized": target.login_normalized, "role": target.role},
        ip_address=ip_address,
    )
    session.delete(target)
