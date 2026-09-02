"""Полный HTTP-флоу входа/выхода через /login и /logout, с ADClient
подменённым через app.dependency_overrides (без реального AD). Пароль,
переданный в форму, никогда не должен попадать в лог сборщика/веб-приложения
(в этих тестах логирование не настроено на файл, поэтому проверяем, что он
хотя бы не оказывается в теле ответа/сообщении об ошибке)."""
from dataclasses import dataclass, field
from typing import List, Optional


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
    """Принимает ровно один известный логин/пароль, остальное -- ADAuthError."""

    def __init__(self, valid_login="ivanov", valid_password="CorrectPass1"):
        self.valid_login = valid_login
        self.valid_password = valid_password

    def authenticate(self, login, password):
        from printaudit.ad.client import ADAuthError
        from printaudit.ad_normalize import normalize_login, split_login

        if normalize_login(login) != normalize_login(self.valid_login) or password != self.valid_password:
            raise ADAuthError("Неверный логин или пароль")
        # Реальный ADClient.authenticate() ВСЕГДА возвращает логин с доменом,
        # взятым из settings.domain, независимо от того, что ввёл пользователь
        # (см. printaudit/ad/client.py) -- этот фейк повторяет то же поведение,
        # а не просто нормализует введённый вариант как есть.
        _typed_domain, sam = split_login(self.valid_login)
        return _FakePrincipal(login_normalized=normalize_login(f"example.local\\{sam}"), sam_account_name=sam)


def _override_ad_client(http_client, fake_client):
    import webapp.main as main
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_ad_client, get_auth_availability_dep

    main.app.dependency_overrides[get_ad_client] = lambda: fake_client
    # Тестовое окружение не задаёт AD_SERVER/AD_BASE_DN (ADSettings.is_configured
    # был бы False), поэтому явно форсируем ad_enabled=True -- эти тесты про сам
    # AD-флоу, а не про то, включён ли AD в принципе (для этого см.
    # tests/test_local_login_http.py).
    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(
        local_enabled=True, ad_enabled=True
    )


def _get_csrf(http_client):
    http_client.get("/login")
    return http_client.cookies.get("pa_csrf")


def test_login_page_renders_with_csrf_token(http_client):
    resp = http_client.get("/login")
    assert resp.status_code == 200
    assert "csrf_token" in resp.text or "pa_csrf" in http_client.cookies


def test_login_with_correct_ad_credentials_but_no_app_user_is_refused(http_client):
    _override_ad_client(http_client, _FakeADClient())
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login", data={"csrf_token": csrf, "login": "ivanov", "password": "CorrectPass1", "provider": "ad", "next": "/"}
    )
    assert resp.status_code == 403
    assert "доступ" in resp.text.lower()


def test_login_with_wrong_password_is_401(http_client):
    _override_ad_client(http_client, _FakeADClient())
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login", data={"csrf_token": csrf, "login": "ivanov", "password": "WrongPassword", "provider": "ad", "next": "/"}
    )
    assert resp.status_code == 401
    assert "wrongpassword" not in resp.text.lower()  # пароль не должен светиться в ответе


def test_login_succeeds_and_sets_session_cookie_when_app_user_exists(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.sessions import SESSION_COOKIE_NAME

    session = SessionLocal()
    session.add(AppUser(login_normalized="example.local\\ivanov", role="viewer", is_active=True))
    session.commit()
    session.close()

    _override_ad_client(http_client, _FakeADClient())
    csrf = _get_csrf(http_client)
    resp = http_client.post(
        "/login",
        data={"csrf_token": csrf, "login": "ivanov", "password": "CorrectPass1", "provider": "ad", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert SESSION_COOKIE_NAME in http_client.cookies

    dashboard = http_client.get("/")
    assert dashboard.status_code == 200


def test_login_normalizes_all_three_formats_to_same_app_user(http_client):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    session.add(AppUser(login_normalized="example.local\\ivanov", role="viewer", is_active=True))
    session.commit()
    session.close()

    for login_variant in ["ivanov", "EXAMPLE.LOCAL\\ivanov", "IVANOV@example.local"]:
        client_module_client = http_client  # один и тот же TestClient, но каждый раз новый логин
        _override_ad_client(client_module_client, _FakeADClient(valid_login=login_variant))
        csrf = _get_csrf(client_module_client)
        resp = client_module_client.post(
            "/login",
            data={"csrf_token": csrf, "login": login_variant, "password": "CorrectPass1", "provider": "ad", "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"login variant failed: {login_variant}"


def test_logout_revokes_session(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="viewer")
    assert http_client.get("/").status_code == 200

    csrf = http_client.cookies.get("pa_csrf")
    resp = http_client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    # Cookie должна быть удалена/невалидна -- дальше как неавторизованный.
    resp2 = http_client.get("/", follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"].startswith("/login")
