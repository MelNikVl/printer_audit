"""printaudit.monitoring.snmp_profiles — CRUD с валидацией конфигурации
SNMPv3/v2c (те же правила, что resolve_snmp_security применит при реальном
опросе, но проверенные сразу при сохранении), аудит-лог."""
import pytest

from printaudit.monitoring.snmp_profiles import (
    SnmpProfileError,
    create_snmp_profile,
    set_snmp_profile_active,
    update_snmp_profile,
)


def _actor(session):
    from printaudit.models import AppUser

    actor = session.query(AppUser).filter_by(login_normalized="domain\\actor").first()
    if actor is None:
        actor = AppUser(login_normalized="domain\\actor", role="admin", is_active=True)
        session.add(actor)
        session.flush()
    return actor


def test_create_v3_no_auth_no_priv(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    profile = create_snmp_profile(session, actor=_actor(session), name="P1", snmp_v3_username="user1")
    session.commit()
    assert profile.snmp_version == "v3"
    assert profile.snmp_v3_username == "user1"
    session.close()


def test_create_v3_without_username_raises(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    with pytest.raises(SnmpProfileError, match="snmp_v3_username"):
        create_snmp_profile(session, actor=_actor(session), name="P2", snmp_version="v3")
    session.close()


def test_create_v3_auth_without_key_env_var_raises(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    with pytest.raises(SnmpProfileError, match="ключом аутентификации"):
        create_snmp_profile(session, actor=_actor(session), name="P3", snmp_v3_username="u", snmp_v3_auth_protocol="SHA")
    session.close()


def test_create_v3_priv_without_auth_raises(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    with pytest.raises(SnmpProfileError, match="authentication"):
        create_snmp_profile(
            session, actor=_actor(session), name="P4", snmp_v3_username="u",
            snmp_v3_priv_protocol="AES", snmp_v3_priv_key_env_var="X",
        )
    session.close()


def test_create_v2c_without_env_var_raises(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    with pytest.raises(SnmpProfileError, match="community"):
        create_snmp_profile(session, actor=_actor(session), name="P5", snmp_version="v2c")
    session.close()


def test_create_v2c_with_env_var_succeeds(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    profile = create_snmp_profile(
        session, actor=_actor(session), name="P6", snmp_version="v2c", credentials_env_var="SNMP_CRED_P6",
    )
    session.commit()
    assert profile.snmp_version == "v2c"
    assert profile.credentials_env_var == "SNMP_CRED_P6"
    session.close()


def test_unknown_version_raises(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    with pytest.raises(SnmpProfileError, match="snmp_version"):
        create_snmp_profile(session, actor=_actor(session), name="P7", snmp_version="v1")
    session.close()


def test_duplicate_name_raises(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    actor = _actor(session)
    create_snmp_profile(session, actor=actor, name="Dup", snmp_v3_username="u")
    session.commit()
    with pytest.raises(SnmpProfileError, match="уже существует"):
        create_snmp_profile(session, actor=actor, name="Dup", snmp_v3_username="u2")
    session.close()


def test_update_profile_changes_fields_and_audits(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AuditLog

    session = SessionLocal()
    actor = _actor(session)
    profile = create_snmp_profile(session, actor=actor, name="P8", snmp_v3_username="u1")
    session.commit()

    update_snmp_profile(session, actor=actor, profile=profile, name="P8", snmp_v3_username="u2")
    session.commit()

    session.close()
    session = SessionLocal()
    try:
        from printaudit.models import SnmpProfile

        updated = session.query(SnmpProfile).filter_by(name="P8").one()
        assert updated.snmp_v3_username == "u2"
        audit_rows = session.query(AuditLog).filter_by(action="snmp_profile.update").all()
        assert len(audit_rows) == 1
    finally:
        session.close()


def test_set_active_toggles_and_audits(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    actor = _actor(session)
    profile = create_snmp_profile(session, actor=actor, name="P9", snmp_v3_username="u")
    session.commit()

    set_snmp_profile_active(session, actor=actor, profile=profile, is_active=False)
    session.commit()
    assert profile.is_active is False

    set_snmp_profile_active(session, actor=actor, profile=profile, is_active=True)
    session.commit()
    assert profile.is_active is True
    session.close()


def test_protocol_names_normalized_to_uppercase(app_env):
    from printaudit.database import SessionLocal

    session = SessionLocal()
    profile = create_snmp_profile(
        session, actor=_actor(session), name="P10", snmp_v3_username="u",
        snmp_v3_auth_protocol="sha256", snmp_v3_auth_key_env_var="X",
    )
    session.commit()
    assert profile.snmp_v3_auth_protocol == "SHA256"
    session.close()
