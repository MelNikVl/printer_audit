"""Тесты серверных сессий: создание, валидация, истечение срока, отзыв."""
from datetime import timedelta

from printaudit.timeutil import utcnow


def _make_app_user(session, AppUser, login="domain\\ivanov", role="viewer"):
    u = AppUser(login_normalized=login, role=role, is_active=True)
    session.add(u)
    session.commit()
    return u


def test_create_and_validate_session(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import create_session, get_app_user_for_token

    session = SessionLocal()
    user = _make_app_user(session, AppUser)

    token = create_session(session, user, ip_address="10.0.0.1", user_agent="pytest")
    assert token

    resolved = get_app_user_for_token(session, token)
    assert resolved is not None
    assert resolved.id == user.id
    session.close()


def test_garbage_token_returns_none(app_env):
    from printaudit.database import SessionLocal
    from printaudit.security.sessions import get_app_user_for_token

    session = SessionLocal()
    assert get_app_user_for_token(session, "not-a-real-token") is None
    assert get_app_user_for_token(session, None) is None
    session.close()


def test_expired_session_is_rejected(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser, WebSession
    from printaudit.security.sessions import create_session, get_app_user_for_token

    session = SessionLocal()
    user = _make_app_user(session, AppUser)
    token = create_session(session, user)

    web_session = session.query(WebSession).filter_by(app_user_id=user.id).one()
    web_session.expires_at = utcnow() - timedelta(minutes=1)
    session.commit()

    assert get_app_user_for_token(session, token) is None
    session.close()


def test_revoked_session_is_rejected(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import create_session, get_app_user_for_token, revoke_session

    session = SessionLocal()
    user = _make_app_user(session, AppUser)
    token = create_session(session, user)
    assert get_app_user_for_token(session, token) is not None

    revoke_session(session, token)
    assert get_app_user_for_token(session, token) is None
    session.close()


def test_revoke_all_sessions_for_user(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import create_session, get_app_user_for_token, revoke_all_sessions_for_user

    session = SessionLocal()
    user = _make_app_user(session, AppUser)
    token_a = create_session(session, user)
    token_b = create_session(session, user)

    revoke_all_sessions_for_user(session, user.id)
    session.commit()

    assert get_app_user_for_token(session, token_a) is None
    assert get_app_user_for_token(session, token_b) is None
    session.close()


def test_two_different_users_get_independent_sessions(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import create_session, get_app_user_for_token

    session = SessionLocal()
    alice = _make_app_user(session, AppUser, login="domain\\alice")
    bob = _make_app_user(session, AppUser, login="domain\\bob")
    token_alice = create_session(session, alice)
    token_bob = create_session(session, bob)

    assert get_app_user_for_token(session, token_alice).login_normalized == "domain\\alice"
    assert get_app_user_for_token(session, token_bob).login_normalized == "domain\\bob"
    session.close()
