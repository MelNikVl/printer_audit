"""Серверные сессии, привязанные к HttpOnly cookie.

В cookie у клиента лежит только случайный "сырой" токен (secrets.token_urlsafe).
В БД (web_sessions.id) хранится не он сам, а его HMAC-хэш с секретом приложения
(SESSION_SECRET_KEY) — то есть утечка БД сама по себе не даёт угнать сессию,
не зная секрета, а утечка секрета без БД тоже ничего не даёт. Пароль
пользователя AD в сессии не участвует и здесь никогда не появляется."""
import hashlib
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from printaudit.ad_settings import get_session_settings
from printaudit.models import AppUser, WebSession
from printaudit.timeutil import naive_utc, utcnow

SESSION_COOKIE_NAME = "pa_session"


def _hash_token(raw_token: str, secret: str) -> str:
    return hashlib.sha256(f"{secret}:{raw_token}".encode("utf-8")).hexdigest()


def create_session(
    session: Session,
    app_user: AppUser,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Создаёт новую сессию и возвращает СЫРОЙ токен для cookie (единственный
    момент, когда он существует в открытом виде за пределами клиента)."""
    settings = get_session_settings()
    raw_token = secrets.token_urlsafe(32)
    now = utcnow()
    web_session = WebSession(
        id=_hash_token(raw_token, settings.secret_key),
        app_user_id=app_user.id,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.lifetime_minutes),
        last_seen_at=now,
        ip_address=ip_address,
        user_agent=(user_agent or None) and user_agent[:300],
    )
    session.add(web_session)
    session.commit()
    return raw_token


def get_app_user_for_token(session: Session, raw_token: Optional[str]) -> Optional[AppUser]:
    """Возвращает AppUser сессии, если токен валиден (существует, не истёк,
    не отозван) — НЕЗАВИСИМО от того, активен ли сам AppUser (is_active
    проверяется отдельно на уровне ролевых проверок, чтобы можно было
    показать понятное "доступ отключён", а не молча выкинуть на логин)."""
    if not raw_token:
        return None
    settings = get_session_settings()
    token_hash = _hash_token(raw_token, settings.secret_key)
    web_session = session.get(WebSession, token_hash)
    if web_session is None:
        return None
    if web_session.revoked_at is not None:
        return None
    now = naive_utc(utcnow())
    if naive_utc(web_session.expires_at) < now:
        return None

    web_session.last_seen_at = utcnow()
    session.commit()
    return session.get(AppUser, web_session.app_user_id)


def revoke_session(session: Session, raw_token: Optional[str]) -> None:
    if not raw_token:
        return
    settings = get_session_settings()
    token_hash = _hash_token(raw_token, settings.secret_key)
    web_session = session.get(WebSession, token_hash)
    if web_session is not None and web_session.revoked_at is None:
        web_session.revoked_at = utcnow()
        session.commit()


def revoke_all_sessions_for_user(session: Session, app_user_id: int) -> None:
    now = utcnow()
    rows = (
        session.query(WebSession)
        .filter(WebSession.app_user_id == app_user_id, WebSession.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
