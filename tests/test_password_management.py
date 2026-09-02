"""printaudit.security.password_management: смена пароля, отзыв всех сессий
после смены (включая ту, из которой пришёл запрос)."""
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


def test_change_password_success(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.password_management import change_password
    from printaudit.security.passwords import verify_password

    session = SessionLocal()
    user = _make_local_user(session, AppUser)

    change_password(session, user=user, current_password="CorrectHorseBattery1", new_password="BrandNewPassword2")

    session.refresh(user)
    assert verify_password("BrandNewPassword2", user.password_hash)
    assert not verify_password("CorrectHorseBattery1", user.password_hash)
    assert user.must_change_password is False
    assert user.password_changed_at is not None
    session.close()


def test_change_password_wrong_current_password_rejected(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.password_management import PasswordChangeError, change_password

    session = SessionLocal()
    user = _make_local_user(session, AppUser)

    with pytest.raises(PasswordChangeError, match="Текущий пароль"):
        change_password(session, user=user, current_password="WrongOne123", new_password="BrandNewPassword2")
    session.close()


def test_change_password_weak_new_password_rejected(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.password_management import PasswordChangeError, change_password

    session = SessionLocal()
    user = _make_local_user(session, AppUser)

    with pytest.raises(PasswordChangeError):
        change_password(session, user=user, current_password="CorrectHorseBattery1", new_password="short")
    session.close()


def test_change_password_same_as_current_rejected(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.password_management import PasswordChangeError, change_password

    session = SessionLocal()
    user = _make_local_user(session, AppUser)

    with pytest.raises(PasswordChangeError, match="отличаться"):
        change_password(
            session, user=user, current_password="CorrectHorseBattery1", new_password="CorrectHorseBattery1"
        )
    session.close()


def test_change_password_rejects_ad_provider_user(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.password_management import PasswordChangeError, change_password

    session = SessionLocal()
    user = AppUser(login_normalized="domain\\aduser", role="viewer", is_active=True, auth_provider="ad")
    session.add(user)
    session.commit()

    with pytest.raises(PasswordChangeError, match="локальных"):
        change_password(session, user=user, current_password="x", new_password="BrandNewPassword2")
    session.close()


def test_change_password_revokes_all_sessions_including_current(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.password_management import change_password
    from printaudit.security.sessions import create_session, get_app_user_for_token

    session = SessionLocal()
    user = _make_local_user(session, AppUser)
    token_a = create_session(session, user)
    token_b = create_session(session, user)
    assert get_app_user_for_token(session, token_a) is not None
    assert get_app_user_for_token(session, token_b) is not None

    change_password(session, user=user, current_password="CorrectHorseBattery1", new_password="BrandNewPassword2")

    assert get_app_user_for_token(session, token_a) is None
    assert get_app_user_for_token(session, token_b) is None
    session.close()


def test_change_password_writes_audit_log_without_passwords(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser, AuditLog
    from printaudit.security.password_management import change_password

    session = SessionLocal()
    user = _make_local_user(session, AppUser)
    change_password(session, user=user, current_password="CorrectHorseBattery1", new_password="BrandNewPassword2")

    entries = session.query(AuditLog).filter_by(action="password.change").all()
    assert len(entries) == 1
    dumped = str(entries[0].old_value) + str(entries[0].new_value)
    assert "CorrectHorseBattery1" not in dumped
    assert "BrandNewPassword2" not in dumped
    session.close()
