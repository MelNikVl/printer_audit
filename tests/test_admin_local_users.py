"""printaudit.admin_users.create_local_user: создание локального пользователя
администратором с временным паролем и обязательной сменой при первом входе."""
import pytest


def _make_app_user(session, AppUser, login, role, is_active=True):
    u = AppUser(login_normalized=login, role=role, is_active=is_active)
    session.add(u)
    session.flush()
    return u


def test_create_local_user_success(app_env):
    from printaudit.admin_users import create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.passwords import verify_password

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    user, temp_password = create_local_user(session, actor=actor, login="newlocal", role="viewer")
    session.commit()

    assert user.auth_provider == "local"
    assert user.must_change_password is True
    assert user.role == "viewer"
    assert len(temp_password) >= 12
    assert verify_password(temp_password, user.password_hash)
    session.close()


def test_create_local_user_temp_password_is_random_each_time(app_env):
    from printaudit.admin_users import create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    _, pw1 = create_local_user(session, actor=actor, login="user1", role="viewer")
    session.commit()
    _, pw2 = create_local_user(session, actor=actor, login="user2", role="viewer")
    session.commit()

    assert pw1 != pw2
    session.close()


def test_admin_cannot_create_local_superadmin(app_env):
    from printaudit.admin_users import AdminActionError, create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\admin1", "admin")
    session.commit()

    with pytest.raises(AdminActionError):
        create_local_user(session, actor=actor, login="newsuper", role="superadmin")
    session.close()


def test_admin_can_create_local_viewer_and_admin(app_env):
    from printaudit.admin_users import create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\admin1", "admin")
    session.commit()

    v, _ = create_local_user(session, actor=actor, login="v1", role="viewer")
    a, _ = create_local_user(session, actor=actor, login="a1", role="admin")
    session.commit()
    assert v.role == "viewer"
    assert a.role == "admin"
    session.close()


def test_create_local_user_rejects_duplicate_login(app_env):
    from printaudit.admin_users import AdminActionError, create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    create_local_user(session, actor=actor, login="dupuser", role="viewer")
    session.commit()

    with pytest.raises(AdminActionError):
        create_local_user(session, actor=actor, login="dupuser", role="admin")
    session.close()


def test_create_local_user_rejects_login_colliding_with_ad_user(app_env):
    from printaudit.admin_users import AdminActionError, create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    _make_app_user(session, AppUser, "domain\\aduser", "viewer")  # AD-provider by default
    session.commit()

    with pytest.raises(AdminActionError):
        create_local_user(session, actor=actor, login="domain\\aduser", role="viewer")
    session.close()


def test_create_local_user_writes_audit_log_without_password(app_env):
    from printaudit.admin_users import create_local_user
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser, AuditLog

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    _, temp_password = create_local_user(session, actor=actor, login="audituser", role="viewer")
    session.commit()

    entries = session.query(AuditLog).filter_by(action="admin.create_local_user").all()
    assert len(entries) == 1
    dumped = str(entries[0].new_value)
    assert temp_password not in dumped
    session.close()
