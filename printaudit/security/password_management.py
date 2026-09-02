"""Смена пароля локальной учётки — и добровольная (пользователь меняет свой
собственный пароль), и принудительная (первый вход с временным паролем).
В обоих случаях: проверка текущего пароля, валидация силы нового,
отзыв ВСЕХ активных сессий этой учётки (включая ту, из которой пришёл сам
запрос смены пароля — намеренно, см. webapp/change_password_routes.py) и
запись в audit_log."""
from typing import Optional

from sqlalchemy.orm import Session

from printaudit import audit
from printaudit.models import AppUser
from printaudit.security.passwords import hash_password, validate_password_strength, verify_password
from printaudit.security.sessions import revoke_all_sessions_for_user
from printaudit.timeutil import utcnow


class PasswordChangeError(Exception):
    """Ожидаемая ошибка (неверный текущий пароль, слабый новый пароль и т.п.) —
    показывается пользователю как есть, не как внутренняя 500."""


def change_password(
    session: Session,
    *,
    user: AppUser,
    current_password: str,
    new_password: str,
    ip_address: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    if user.auth_provider != "local":
        raise PasswordChangeError("Смена пароля доступна только для локальных учётных записей.")
    if not verify_password(current_password, user.password_hash or ""):
        raise PasswordChangeError("Текущий пароль указан неверно.")
    if new_password == current_password:
        raise PasswordChangeError("Новый пароль должен отличаться от текущего.")
    try:
        validate_password_strength(new_password)
    except ValueError as exc:
        raise PasswordChangeError(str(exc)) from exc

    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None

    # Отзываем ВСЕ сессии, включая текущую -- если пароль/сессию украли,
    # смена пароля должна гарантированно выбить и вора, и легитимного
    # пользователя (которому придётся просто войти заново новым паролем).
    revoke_all_sessions_for_user(session, user.id)

    audit.record(
        session,
        actor_app_user_id=user.id,
        action="password.change",
        object_type="app_user",
        object_id=user.id,
        ip_address=ip_address,
        session_id=session_id,
    )
    session.commit()
