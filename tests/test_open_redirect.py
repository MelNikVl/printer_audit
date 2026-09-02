"""Защита от open redirect через next=/POST /login. Юнит-тесты на
safe_next_path() плюс сквозные HTTP-тесты через реальный флоу логина
(мокнутый AD, как в test_login_flow.py)."""
from dataclasses import dataclass, field
from typing import List, Optional

import pytest


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("/admin", "/admin"),
        ("/admin/departments?msg=ok", "/admin/departments?msg=ok"),
        ("/by-user", "/by-user"),
        # protocol-relative
        ("//evil.com", "/"),
        ("///evil.com", "/"),
        ("//evil.com/path", "/"),
        # схема/хост без ведущего слэша
        ("https://evil.com", "/"),
        ("http://evil.com", "/"),
        ("javascript:alert(1)", "/"),
        ("evil.com", "/"),
        # backslash-варианты (браузер трактует '\\' как '/')
        ("/\\evil.com", "/"),
        ("\\\\evil.com", "/"),
        ("/\\/evil.com", "/"),
        # пробельные/управляющие символы между слэшами
        ("/\t/evil.com", "/"),
        ("/\n/evil.com", "/"),
        ("/ /evil.com", "/"),
        (" /admin", "/admin"),  # обычные пробелы по краям — не атака, просто trim
    ],
)
def test_safe_next_path(raw, expected):
    from webapp.deps import safe_next_path

    assert safe_next_path(raw) == expected


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


class _FakeADClient:
    def authenticate(self, login, password):
        from printaudit.ad_normalize import split_login

        # Реальный ADClient.authenticate() всегда строит UPN из settings.domain,
        # независимо от того, что ввёл пользователь (см. printaudit/ad/client.py) —
        # этот фейк повторяет то же поведение, а не сравнивает введённый вариант как есть.
        _typed_domain, sam = split_login(login)
        if sam.lower() != "ivanov" or password != "CorrectPass1":
            from printaudit.ad.client import ADAuthError

            raise ADAuthError("bad creds")
        return _FakePrincipal(login_normalized="example.local\\ivanov", sam_account_name="ivanov")


def _setup_app_user_and_override(http_client):
    import webapp.main as main
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from webapp.deps import get_ad_client

    session = SessionLocal()
    session.add(AppUser(login_normalized="example.local\\ivanov", role="viewer", is_active=True))
    session.commit()
    session.close()

    main.app.dependency_overrides[get_ad_client] = lambda: _FakeADClient()


def _login(http_client, next_value):
    http_client.get("/login")
    csrf = http_client.cookies.get("pa_csrf")
    return http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "ivanov", "password": "CorrectPass1", "next": next_value},
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    "malicious_next",
    [
        "https://evil.com",
        "http://evil.com/phish",
        "//evil.com",
        "///evil.com",
        "/\\evil.com",
        "\\\\evil.com",
        "javascript:alert(document.cookie)",
    ],
)
def test_post_login_never_redirects_off_site(http_client, malicious_next):
    _setup_app_user_and_override(http_client)
    resp = _login(http_client, malicious_next)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == "/", f"next={malicious_next!r} leaked into redirect: {location!r}"


def test_post_login_preserves_legitimate_local_next(http_client):
    _setup_app_user_and_override(http_client)
    resp = _login(http_client, "/admin/departments")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/departments"


def test_get_login_sanitizes_next_in_rendered_form(http_client):
    resp = http_client.get("/login", params={"next": "https://evil.com"})
    assert resp.status_code == 200
    assert "evil.com" not in resp.text
    assert 'name="next" value="/"' in resp.text


def test_get_login_preserves_legitimate_next_in_rendered_form(http_client):
    resp = http_client.get("/login", params={"next": "/admin"})
    assert resp.status_code == 200
    assert 'name="next" value="/admin"' in resp.text
