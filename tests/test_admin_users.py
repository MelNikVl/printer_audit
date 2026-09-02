"""Тесты гарантий безопасности назначения ролей: admin не может назначить
superadmin, нельзя изменить собственную роль, нельзя понизить/удалить
последнего активного superadmin. Это тестирует инвариант данных напрямую в
сервисном слое (printaudit.admin_users) -- независимо от него, на уровне
HTTP-роутов /admin/administrators должен быть доступен только role=superadmin
(проверяется в tests/test_auth_roles.py), так что в реальном приложении actor
с ролью admin до этих функций вообще не доходит."""
import pytest


def _make_app_user(session, AppUser, login, role, is_active=True):
    u = AppUser(login_normalized=login, role=role, is_active=is_active)
    session.add(u)
    session.flush()
    return u


def test_admin_cannot_assign_superadmin_role(app_env):
    from printaudit.admin_users import AdminActionError, upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor_admin = _make_app_user(session, AppUser, "domain\\admin1", "admin")
    session.commit()

    with pytest.raises(AdminActionError):
        upsert_admin_assignment(session, actor=actor_admin, login_normalized="domain\\newuser", role="superadmin")
    session.close()


def test_superadmin_can_assign_superadmin_role(app_env):
    from printaudit.admin_users import upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor_super = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    result = upsert_admin_assignment(session, actor=actor_super, login_normalized="domain\\newuser", role="superadmin")
    session.commit()
    assert result.role == "superadmin"
    session.close()


def test_admin_can_assign_viewer_and_admin_roles(app_env):
    from printaudit.admin_users import upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor_admin = _make_app_user(session, AppUser, "domain\\admin1", "admin")
    session.commit()

    v = upsert_admin_assignment(session, actor=actor_admin, login_normalized="domain\\viewer1", role="viewer")
    a = upsert_admin_assignment(session, actor=actor_admin, login_normalized="domain\\admin2", role="admin")
    session.commit()
    assert v.role == "viewer"
    assert a.role == "admin"
    session.close()


def test_cannot_change_own_role(app_env):
    from printaudit.admin_users import AdminActionError, upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor_super = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    with pytest.raises(AdminActionError):
        upsert_admin_assignment(session, actor=actor_super, login_normalized="domain\\super1", role="viewer")
    session.close()


def test_cannot_demote_last_active_superadmin(app_env):
    from printaudit.admin_users import AdminActionError, upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    # actor отдельный от target -- изолирует правило "последний superadmin" от
    # отдельного правила "нельзя менять свою роль" (см. docstring файла).
    actor = _make_app_user(session, AppUser, "domain\\someone", "admin")
    only_superadmin = _make_app_user(session, AppUser, "domain\\lastsuper", "superadmin")
    session.commit()

    with pytest.raises(AdminActionError):
        upsert_admin_assignment(session, actor=actor, login_normalized="domain\\lastsuper", role="viewer")
    session.close()


def test_can_demote_superadmin_when_another_active_superadmin_exists(app_env):
    from printaudit.admin_users import upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super_a", "superadmin")
    target = _make_app_user(session, AppUser, "domain\\super_b", "superadmin")
    session.commit()

    result = upsert_admin_assignment(session, actor=actor, login_normalized="domain\\super_b", role="admin")
    session.commit()
    assert result.role == "admin"
    session.close()


def test_demoting_already_inactive_superadmin_is_allowed(app_env):
    """Понижение УЖЕ отключённого superadmin не должно блокироваться правилом
    "последний активный" -- он и так не в счёт активных."""
    from printaudit.admin_users import upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\admin1", "admin")
    inactive_super = _make_app_user(session, AppUser, "domain\\inactive_super", "superadmin", is_active=False)
    session.commit()

    result = upsert_admin_assignment(session, actor=actor, login_normalized="domain\\inactive_super", role="viewer")
    session.commit()
    assert result.role == "viewer"
    assert result.is_active is True  # upsert реактивирует назначение
    session.close()


def test_cannot_disable_last_active_superadmin(app_env):
    from printaudit.admin_users import AdminActionError, disable_admin
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\someone", "admin")
    only_superadmin = _make_app_user(session, AppUser, "domain\\lastsuper", "superadmin")
    session.commit()

    with pytest.raises(AdminActionError):
        disable_admin(session, actor=actor, target=only_superadmin)
    session.close()


def test_cannot_delete_last_active_superadmin(app_env):
    from printaudit.admin_users import AdminActionError, delete_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\someone", "admin")
    only_superadmin = _make_app_user(session, AppUser, "domain\\lastsuper", "superadmin")
    session.commit()

    with pytest.raises(AdminActionError):
        delete_admin_assignment(session, actor=actor, target=only_superadmin)
    session.close()


def test_cannot_disable_self(app_env):
    from printaudit.admin_users import AdminActionError, disable_admin
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    other = _make_app_user(session, AppUser, "domain\\super2", "superadmin")
    session.commit()

    with pytest.raises(AdminActionError):
        disable_admin(session, actor=actor, target=actor)
    session.close()


def test_admin_action_writes_audit_log(app_env):
    from printaudit.admin_users import upsert_admin_assignment
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser, AuditLog

    session = SessionLocal()
    actor = _make_app_user(session, AppUser, "domain\\super1", "superadmin")
    session.commit()

    upsert_admin_assignment(session, actor=actor, login_normalized="domain\\newuser", role="viewer")
    session.commit()

    import json

    entries = session.query(AuditLog).filter_by(action="admin.create").all()
    assert len(entries) == 1
    assert entries[0].actor_app_user_id == actor.id
    assert json.loads(entries[0].new_value)["login_normalized"] == "domain\\newuser"
    session.close()
