"""printaudit.security.local_auth: успешный/неуспешный вход, lockout после
серии неверных попыток, что пароль нигде не хранится в открытом виде."""
from datetime import timedelta

import pytest


def _make_local_user(session, AppUser, login="localviewer", password="CorrectHorseBattery1", role="viewer"):
    from printaudit.security.passwords import hash_password

    user = AppUser(
        login_normalized=login, role=role, is_active=True,
        auth_provider="local", password_hash=hash_password(password),
    )
    session.add(user)
    session.commit()
    return user


def test_successful_local_login(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import authenticate_local

    session = SessionLocal()
    _make_local_user(session, AppUser)

    result = authenticate_local(session, "localviewer", "CorrectHorseBattery1")
    assert result.login_normalized == "localviewer"
    session.close()


def test_failed_local_login_wrong_password(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import LocalAuthInvalidCredentialsError, authenticate_local

    session = SessionLocal()
    _make_local_user(session, AppUser)

    with pytest.raises(LocalAuthInvalidCredentialsError):
        authenticate_local(session, "localviewer", "WrongPassword123")
    session.close()


def test_failed_local_login_unknown_user(app_env):
    from printaudit.database import SessionLocal
    from printaudit.security.local_auth import LocalAuthInvalidCredentialsError, authenticate_local

    session = SessionLocal()
    with pytest.raises(LocalAuthInvalidCredentialsError):
        authenticate_local(session, "ghost", "WhateverPassword1")
    session.close()


def test_ad_provider_user_cannot_login_via_local_auth(app_env):
    """auth_provider="ad" учётка не должна проходить через authenticate_local,
    даже если бы у неё каким-то образом оказался password_hash."""
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import LocalAuthInvalidCredentialsError, authenticate_local
    from printaudit.security.passwords import hash_password

    session = SessionLocal()
    session.add(
        AppUser(
            login_normalized="domain\\aduser", role="viewer", is_active=True,
            auth_provider="ad", password_hash=hash_password("SomePassword123"),
        )
    )
    session.commit()

    with pytest.raises(LocalAuthInvalidCredentialsError):
        authenticate_local(session, "domain\\aduser", "SomePassword123")
    session.close()


def test_lockout_after_threshold_failed_attempts(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import (
        LOCKOUT_THRESHOLD,
        LocalAuthInvalidCredentialsError,
        LocalAuthLockedError,
        authenticate_local,
    )

    session = SessionLocal()
    _make_local_user(session, AppUser)

    for _ in range(LOCKOUT_THRESHOLD):
        with pytest.raises(LocalAuthInvalidCredentialsError):
            authenticate_local(session, "localviewer", "WrongPassword123")

    # Следующая попытка -- даже с ПРАВИЛЬНЫМ паролем -- должна быть заблокирована.
    with pytest.raises(LocalAuthLockedError):
        authenticate_local(session, "localviewer", "CorrectHorseBattery1")
    session.close()


def test_lockout_resets_failed_count_after_expiry(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import LOCKOUT_THRESHOLD, LocalAuthInvalidCredentialsError, authenticate_local
    from printaudit.timeutil import utcnow

    session = SessionLocal()
    user = _make_local_user(session, AppUser)
    for _ in range(LOCKOUT_THRESHOLD):
        with pytest.raises(LocalAuthInvalidCredentialsError):
            authenticate_local(session, "localviewer", "WrongPassword123")

    # Имитируем, что блокировка уже истекла.
    session.refresh(user)
    user.locked_until = utcnow() - timedelta(seconds=1)
    session.commit()

    # Успешный вход правильным паролем должен снова работать.
    result = authenticate_local(session, "localviewer", "CorrectHorseBattery1")
    assert result.login_normalized == "localviewer"
    assert result.failed_login_count == 0
    assert result.locked_until is None
    session.close()


def test_successful_login_resets_failed_count(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import LocalAuthInvalidCredentialsError, authenticate_local

    session = SessionLocal()
    _make_local_user(session, AppUser)

    for _ in range(2):
        with pytest.raises(LocalAuthInvalidCredentialsError):
            authenticate_local(session, "localviewer", "WrongPassword123")

    result = authenticate_local(session, "localviewer", "CorrectHorseBattery1")
    assert result.failed_login_count == 0
    session.close()


def test_password_never_stored_in_plaintext_anywhere_in_db(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.local_auth import authenticate_local

    plaintext = "SuperSecretPlaintext123"
    session = SessionLocal()
    _make_local_user(session, AppUser, password=plaintext)
    authenticate_local(session, "localviewer", plaintext)

    row = session.query(AppUser).filter_by(login_normalized="localviewer").one()
    assert plaintext not in (row.password_hash or "")
    session.close()
