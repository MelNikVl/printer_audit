"""Тесты /admin/snmp-profiles: RBAC, создание v3/v2c профилей с валидацией
(нет username -> ошибка для v3, нет community env var -> ошибка для v2c,
priv без auth -> ошибка), редактирование, включение/отключение."""
from tests.conftest import login_as


def _csrf(http_client, url):
    http_client.get(url)
    return http_client.cookies.get("pa_csrf")


def test_admin_snmp_profiles_forbidden_for_viewer(http_client):
    login_as(http_client, role="viewer")
    assert http_client.get("/admin/snmp-profiles").status_code == 403


def test_create_v3_profile_with_auth_priv(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    resp = http_client.post(
        "/admin/snmp-profiles/create",
        data={
            "csrf_token": csrf, "name": "HP-Family", "snmp_version": "v3",
            "snmp_v3_username": "monitor-user", "snmp_v3_auth_protocol": "SHA256",
            "snmp_v3_auth_key_env_var": "SNMP_AUTH_HP", "snmp_v3_priv_protocol": "AES256",
            "snmp_v3_priv_key_env_var": "SNMP_PRIV_HP",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import SnmpProfile

    session = SessionLocal()
    try:
        profile = session.query(SnmpProfile).filter_by(name="HP-Family").one()
        assert profile.snmp_version == "v3"
        assert profile.snmp_v3_username == "monitor-user"
        assert profile.snmp_v3_auth_protocol == "SHA256"
        assert profile.snmp_v3_priv_protocol == "AES256"
    finally:
        session.close()


def test_create_v3_profile_without_username_shows_error(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    resp = http_client.post(
        "/admin/snmp-profiles/create",
        data={"csrf_token": csrf, "name": "Bad-Profile", "snmp_version": "v3"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import SnmpProfile

    session = SessionLocal()
    try:
        assert session.query(SnmpProfile).filter_by(name="Bad-Profile").first() is None
    finally:
        session.close()


def test_create_v3_profile_with_priv_but_no_auth_shows_error(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    resp = http_client.post(
        "/admin/snmp-profiles/create",
        data={
            "csrf_token": csrf, "name": "Priv-No-Auth", "snmp_version": "v3",
            "snmp_v3_username": "u", "snmp_v3_priv_protocol": "AES",
            "snmp_v3_priv_key_env_var": "SOME_ENV",
        },
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_create_v2c_profile_without_env_var_shows_error(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    resp = http_client.post(
        "/admin/snmp-profiles/create",
        data={"csrf_token": csrf, "name": "Legacy-No-Env", "snmp_version": "v2c"},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_create_v2c_profile_with_env_var_succeeds(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    resp = http_client.post(
        "/admin/snmp-profiles/create",
        data={
            "csrf_token": csrf, "name": "Legacy-Printer", "snmp_version": "v2c",
            "credentials_env_var": "SNMP_CRED_LEGACY",
        },
        follow_redirects=False,
    )
    assert "msg=" in resp.headers["location"]


def test_duplicate_profile_name_rejected(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    data = {"csrf_token": csrf, "name": "Dup-Profile", "snmp_version": "v3", "snmp_v3_username": "u"}
    http_client.post("/admin/snmp-profiles/create", data=data, follow_redirects=False)
    resp = http_client.post("/admin/snmp-profiles/create", data=data, follow_redirects=False)
    assert "err=" in resp.headers["location"]


def test_update_and_disable_enable_profile(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    http_client.post(
        "/admin/snmp-profiles/create",
        data={"csrf_token": csrf, "name": "Editable", "snmp_version": "v3", "snmp_v3_username": "u1"},
        follow_redirects=False,
    )

    from printaudit.database import SessionLocal
    from printaudit.models import SnmpProfile

    session = SessionLocal()
    profile_id = session.query(SnmpProfile).filter_by(name="Editable").one().id
    session.close()

    csrf = _csrf(http_client, "/admin/snmp-profiles")
    resp = http_client.post(
        f"/admin/snmp-profiles/{profile_id}/update",
        data={"csrf_token": csrf, "name": "Editable", "snmp_version": "v3", "snmp_v3_username": "u2"},
        follow_redirects=False,
    )
    assert "msg=" in resp.headers["location"]

    session = SessionLocal()
    try:
        profile = session.get(SnmpProfile, profile_id)
        assert profile.snmp_v3_username == "u2"
        assert profile.is_active is True
    finally:
        session.close()

    csrf = _csrf(http_client, "/admin/snmp-profiles")
    http_client.post(f"/admin/snmp-profiles/{profile_id}/disable", data={"csrf_token": csrf}, follow_redirects=False)
    session = SessionLocal()
    try:
        assert session.get(SnmpProfile, profile_id).is_active is False
    finally:
        session.close()

    csrf = _csrf(http_client, "/admin/snmp-profiles")
    http_client.post(f"/admin/snmp-profiles/{profile_id}/enable", data={"csrf_token": csrf}, follow_redirects=False)
    session = SessionLocal()
    try:
        assert session.get(SnmpProfile, profile_id).is_active is True
    finally:
        session.close()


def test_snmp_profiles_page_lists_created_profile(http_client):
    login_as(http_client, role="admin")
    csrf = _csrf(http_client, "/admin/snmp-profiles")
    http_client.post(
        "/admin/snmp-profiles/create",
        data={"csrf_token": csrf, "name": "Listed-Profile", "snmp_version": "v3", "snmp_v3_username": "u"},
        follow_redirects=False,
    )
    resp = http_client.get("/admin/snmp-profiles")
    assert resp.status_code == 200
    assert "Listed-Profile" in resp.text
