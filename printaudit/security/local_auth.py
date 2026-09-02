"""Локальный провайдер аутентификации — независимый от AD. Хранит только
Argon2id-хэш пароля (см. printaudit.security.passwords); блокирует учётку на
время после серии неверных попыток."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from printaudit.ad_normalize import normalize_login
from printaudit.models import AppUser
from printaudit.security.passwords import verify_against_dummy_hash, verify_password
from printaudit.timeutil import naive_utc, utcnow

LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION_MINUTES = 15


class LocalAuthError(Exception):
    """Базовый класс ошибок локального входа."""


class LocalAuthInvalidCredentialsError(LocalAuthError):
    """Неверный логин или пароль (намеренно один и тот же класс/сообщение
    для "нет такого пользователя" и "пароль неверный" — не давать угадывать,
    какие локальные логины существуют)."""


@dataclass
class LocalAuthLockedError(LocalAuthError):
    """Учётка временно заблокирована после серии неверных попыток."""

    locked_until: object  # datetime


def authenticate_local(session: Session, login: str, password: str) -> AppUser:
    """Проверяет логин/пароль локальной учётки. Обновляет и коммитит
    failed_login_count/locked_until по ходу дела (в т.ч. при неудаче) —
    вызывающий код НЕ должен сам коммитить эти поля повторно."""
    login_normalized = normalize_login(login)
    user = (
        session.query(AppUser)
        .filter_by(login_normalized=login_normalized, auth_provider="local")
        .first()
    )

    if user is None:
        # Тратим сопоставимое время на хэш-сравнение, чтобы по задержке
        # ответа нельзя было отличить "такого логина нет" от "пароль неверный".
        verify_against_dummy_hash(password)
        raise LocalAuthInvalidCredentialsError()

    now = utcnow()
    if user.locked_until and naive_utc(user.locked_until) > naive_utc(now):
        raise LocalAuthLockedError(locked_until=user.locked_until)

    if not verify_password(password, user.password_hash or ""):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= LOCKOUT_THRESHOLD:
            user.locked_until = now + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
            user.failed_login_count = 0
        session.commit()
        raise LocalAuthInvalidCredentialsError()

    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0
        user.locked_until = None
        session.commit()

    return user


def lockout_remaining_seconds(user: AppUser) -> Optional[int]:
    """Сколько ещё секунд действует блокировка, или None если не заблокирован.
    Используется для сообщения пользователю ("попробуйте через N минут")."""
    if not user.locked_until:
        return None
    remaining = (naive_utc(user.locked_until) - naive_utc(utcnow())).total_seconds()
    return max(0, int(remaining)) if remaining > 0 else None
