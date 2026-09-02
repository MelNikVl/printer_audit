"""HTTP-уровень: логин-форма с выбором провайдера, полный флоу локального
входа (успех/неудача/lockout/принудительная смена пароля), RBAC для
локальных пользователей, и что при AD_AUTH_ENABLED=false код НИ РАЗУ не
обращается к LDAP (используется ADClient-шпион, который бросает
AssertionError при любом вызове)."""
from dataclasses import dataclass, field
from typing import List, Optional

import pytest


def _override_auth_availability(local_enabled: bool, ad_enabled: bool):
    import webapp.main as main
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_auth_availability_dep

    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(
        local_enabled=local_enabled, ad_enabled=ad_enabled
    )


class _AssertNeverCalledADClient:
    """Любой вызов бросает AssertionError -- используется, чтобы доказать,
    что при отключённом AD код НИ РАЗУ не обращается к LDAP, а не просто
    "обращается и получает ошибку, которая где-то гасится"."""

    def authenticate(self, login, password):
        raise AssertionError("ADClient.authenticate() не должен вызываться при AD_AUTH_ENABLED=false")

    def search_users(self, query, limit=25):
        raise AssertionError("ADClient.search_users() не должен вызываться при AD_AUTH_ENABLED=false")

    def search_groups(self, query, limit=25):
        raise AssertionError("ADClient.search_groups() не должен вызываться при AD_AUTH_ENABLED=false")

    def get_group_members(self, group_dn):
        raise AssertionError("ADClient.get_group_members() не должен вызываться при AD_AUTH_ENABLED=false")

    def get_user_by_login(self, login):
        raise AssertionError("ADClient.get_user_by_login() не должен вызываться при AD_AUTH_ENABLED=false")


def _install_spy_ad_client(http_client):
    import webapp.main as main
    from webapp.deps import get_ad_client

    main.app.dependency_overrides[get_ad_client] = lambda: _AssertNeverCalledADClient()


def _create_local_user(login="localviewer", password="CorrectHorseBattery1", role="viewer", must_change=False):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.passwords import hash_password

    session = SessionLocal()
    user = AppUser(
        login_normalized=login, role=role, is_active=True, auth_provider="local",
        password_hash=hash_password(password), must_change_password=must_change,
    )
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()
    return user_id


def _get_csrf(http_client, path="/login"):
    http_client.get(path)
    return http_client.cookies.get("pa_csrf")


# ---------------------------------------------------------------------------
# Страница логина: какие провайдеры показаны
# ---------------------------------------------------------------------------


def test_login_page_shows_only_local_when_ad_disabled(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    resp = http_client.get("/login")
    assert resp.status_code == 200
    assert "Active Directory" not in resp.text


def test_login_page_shows_both_tabs_when_both_enabled(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=True)
    resp = http_client.get("/login")
    assert resp.status_code == 200
    assert "Локальная учётная запись" in resp.text
    assert "Active Directory" in resp.text


def test_login_page_shows_error_when_nothing_enabled(http_client):
    _override_auth_availability(local_enabled=False, ad_enabled=False)
    resp = http_client.get("/login")
    assert resp.status_code == 200
    assert "недоступен" in resp.text.lower()


# ---------------------------------------------------------------------------
# Успешный/неуспешный локальный вход
# ---------------------------------------------------------------------------


def test_successful_local_login_http(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _create_local_user()
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "CorrectHorseBattery1", "provider": "local", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert http_client.get("/").status_code == 200


def test_failed_local_login_http_wrong_password(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _create_local_user()
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "WrongPassword1", "provider": "local", "next": "/"},
    )
    assert resp.status_code == 401
    assert "Неверный логин" in resp.text


def test_local_login_disabled_returns_clean_rejection(http_client):
    _override_auth_availability(local_enabled=False, ad_enabled=True)
    _create_local_user()
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "CorrectHorseBattery1", "provider": "local", "next": "/"},
    )
    assert resp.status_code == 403
    assert "отключ" in resp.text.lower()


def test_lockout_via_http(http_client):
    from printaudit.security.local_auth import LOCKOUT_THRESHOLD

    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _create_local_user()
    csrf = _get_csrf(http_client)

    for _ in range(LOCKOUT_THRESHOLD):
        http_client.post(
            "/login",
            data={"csrf_token": csrf, "login": "localviewer", "password": "WrongPassword1", "provider": "local", "next": "/"},
        )

    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "CorrectHorseBattery1", "provider": "local", "next": "/"},
    )
    assert resp.status_code == 401
    assert "заблокирован" in resp.text.lower()


# ---------------------------------------------------------------------------
# Принудительная смена пароля
# ---------------------------------------------------------------------------


def test_forced_password_change_redirects_everywhere_until_changed(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _create_local_user(must_change=True)
    csrf = _get_csrf(http_client)
    http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "CorrectHorseBattery1", "provider": "local", "next": "/"},
        follow_redirects=False,
    )

    resp = http_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"

    resp2 = http_client.get("/admin", follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/change-password"

    # /change-password само остаётся доступным.
    assert http_client.get("/change-password").status_code == 200


def test_full_change_password_flow_via_http(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _create_local_user(password="TempPassword123", must_change=True)
    csrf = _get_csrf(http_client)
    http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "TempPassword123", "provider": "local", "next": "/"},
        follow_redirects=False,
    )

    csrf2 = http_client.cookies.get("pa_csrf")
    resp = http_client.post(
        "/change-password",
        data={
            "csrf_token": csrf2, "current_password": "TempPassword123",
            "new_password": "BrandNewPassword2", "new_password_confirm": "BrandNewPassword2",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")

    # Старая сессия отозвана -- дальше как неавторизованный.
    resp2 = http_client.get("/", follow_redirects=False)
    assert resp2.status_code == 303

    # Новый пароль реально работает.
    csrf3 = _get_csrf(http_client)
    resp3 = http_client.post(
        "/login",
        data={"csrf_token": csrf3, "login": "localviewer", "password": "BrandNewPassword2", "provider": "local", "next": "/"},
        follow_redirects=False,
    )
    assert resp3.status_code == 303


def test_change_password_mismatched_confirmation_rejected(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _create_local_user(password="TempPassword123", must_change=True)
    csrf = _get_csrf(http_client)
    http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "localviewer", "password": "TempPassword123", "provider": "local", "next": "/"},
        follow_redirects=False,
    )
    csrf2 = http_client.cookies.get("pa_csrf")
    resp = http_client.post(
        "/change-password",
        data={
            "csrf_token": csrf2, "current_password": "TempPassword123",
            "new_password": "BrandNewPassword2", "new_password_confirm": "SomethingElse3",
        },
    )
    assert resp.status_code == 400
    assert "совпадают" in resp.text.lower()


# ---------------------------------------------------------------------------
# RBAC для локальных пользователей
# ---------------------------------------------------------------------------


def test_local_viewer_cannot_access_admin(http_client):
    from tests.conftest import login_as

    user_id = _create_local_user(role="viewer")
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import SESSION_COOKIE_NAME, create_session

    session = SessionLocal()
    user = session.get(AppUser, user_id)
    token = create_session(session, user)
    session.close()
    http_client.cookies.set(SESSION_COOKIE_NAME, token)

    assert http_client.get("/admin").status_code == 403


def test_local_superadmin_has_full_admin_access(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import SESSION_COOKIE_NAME, create_session

    user_id = _create_local_user(login="localsuper", role="superadmin")
    session = SessionLocal()
    user = session.get(AppUser, user_id)
    token = create_session(session, user)
    session.close()
    http_client.cookies.set(SESSION_COOKIE_NAME, token)

    for path in [
        "/admin", "/admin/departments", "/admin/ad-users", "/admin/ad-groups",
        "/admin/printers", "/admin/pricing", "/admin/administrators",
        "/", "/by-department", "/by-user", "/by-printer",
    ]:
        resp = http_client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# AD полностью выключен: ни одного обращения к LDAP
# ---------------------------------------------------------------------------


def test_ad_disabled_post_login_never_touches_ldap(http_client):
    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _install_spy_ad_client(http_client)
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "someone", "password": "whatever123", "provider": "ad", "next": "/"},
    )
    assert resp.status_code == 403  # не 500 -- AssertionError из шпиона НЕ был брошен


def test_ad_disabled_admin_pages_show_notice_and_never_touch_ldap(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import SESSION_COOKIE_NAME, create_session

    _override_auth_availability(local_enabled=True, ad_enabled=False)
    _install_spy_ad_client(http_client)

    user_id = _create_local_user(login="admin1", role="superadmin")
    session = SessionLocal()
    user = session.get(AppUser, user_id)
    token = create_session(session, user)
    session.close()
    http_client.cookies.set(SESSION_COOKIE_NAME, token)

    for path in ["/admin/administrators", "/admin/ad-users", "/admin/ad-groups"]:
        resp = http_client.get(path, params={"q": "ivan"})
        assert resp.status_code == 200
        assert "отключ" in resp.text.lower()


# ---------------------------------------------------------------------------
# Существующая AD-аутентификация не сломана (регрессия)
# ---------------------------------------------------------------------------


@dataclass
class _FakePrincipal:
    login_normalized: str
    sam_account_name: str
    sid: Optional[str] = "S-1-5-21-1-2-3-1001"
    object_guid: Optional[str] = "guid-1"
    display_name: Optional[str] = "Ivan Ivanov"
    email: Optional[str] = "ivanov@example.local"
    domain: Optional[str] = "example.local"
    dn: str = "cn=ivan,dc=example,dc=local"
    group_dns: List[str] = field(default_factory=list)


class _FakeWorkingADClient:
    def authenticate(self, login, password):
        from printaudit.ad.client import ADAuthError
        from printaudit.ad_normalize import split_login

        _domain, sam = split_login(login)
        if sam.lower() != "ivanov" or password != "CorrectPass1":
            raise ADAuthError("bad creds")
        return _FakePrincipal(login_normalized="example.local\\ivanov", sam_account_name="ivanov")


def test_ad_login_still_works_when_both_providers_enabled(http_client):
    import webapp.main as main
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from webapp.deps import get_ad_client

    _override_auth_availability(local_enabled=True, ad_enabled=True)
    main.app.dependency_overrides[get_ad_client] = lambda: _FakeWorkingADClient()

    session = SessionLocal()
    session.add(AppUser(login_normalized="example.local\\ivanov", role="viewer", is_active=True, auth_provider="ad"))
    session.commit()
    session.close()

    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "ivanov", "password": "CorrectPass1", "provider": "ad", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert http_client.get("/").status_code == 200
