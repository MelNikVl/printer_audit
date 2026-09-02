"""Сырые исключения (LDAP/AD, обнаружение принтеров) не должны попадать в
браузер -- ни адрес AD-сервера, ни код ошибки, ни путь к скрипту. UI должен
показывать нейтральное сообщение с ID ошибки; полный текст -- только в лог
(через logging, проверяем via caplog)."""
import logging

SENSITIVE_DETAIL = "dc01.internal.corp.example:636 refused connection, LDAP result code 52"


def test_safe_error_message_hides_detail_and_logs_it(caplog):
    from webapp.errors import safe_error_message

    with caplog.at_level(logging.ERROR, logger="webapp.errors"):
        shown = safe_error_message(RuntimeError(SENSITIVE_DETAIL), "тестовая операция")

    assert SENSITIVE_DETAIL not in shown
    assert "ID ошибки" in shown
    assert "тестовая операция" in shown

    # Полная информация (включая traceback) должна быть в логе.
    assert SENSITIVE_DETAIL in caplog.text


def test_safe_error_message_ids_are_unique():
    from webapp.errors import safe_error_message

    a = safe_error_message(RuntimeError("x"), "op")
    b = safe_error_message(RuntimeError("x"), "op")
    assert a != b


def test_login_ad_error_does_not_leak_server_details_to_browser(http_client, caplog):
    import webapp.main as main
    from printaudit.ad.client import ADError
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_ad_client, get_auth_availability_dep

    class _FailingADClient:
        def authenticate(self, login, password):
            raise ADError(f"Не удалось подключиться к AD: {SENSITIVE_DETAIL}")

    main.app.dependency_overrides[get_ad_client] = lambda: _FailingADClient()
    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(
        local_enabled=True, ad_enabled=True
    )

    http_client.get("/login")
    csrf = http_client.cookies.get("pa_csrf")
    with caplog.at_level(logging.ERROR, logger="webapp.errors"):
        resp = http_client.post(
            "/login", data={"csrf_token": csrf, "login": "ivanov", "password": "x", "provider": "ad", "next": "/"}
        )

    assert resp.status_code == 503
    assert SENSITIVE_DETAIL not in resp.text
    assert "dc01.internal.corp.example" not in resp.text
    assert "ID ошибки" in resp.text

    assert SENSITIVE_DETAIL in caplog.text


def test_admin_ad_user_search_error_does_not_leak_to_page(http_client):
    from tests.conftest import login_as
    import webapp.main as main
    from printaudit.ad.client import ADError
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_ad_client, get_auth_availability_dep

    login_as(http_client, role="admin")

    class _FailingSearch:
        def search_users(self, query, limit=25):
            raise ADError(f"bind failed: {SENSITIVE_DETAIL}")

    main.app.dependency_overrides[get_ad_client] = lambda: _FailingSearch()
    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(local_enabled=True, ad_enabled=True)

    resp = http_client.get("/admin/ad-users", params={"q": "ivan"})
    assert resp.status_code == 200
    assert SENSITIVE_DETAIL not in resp.text
    assert "ID ошибки" in resp.text


def test_admin_administrators_search_error_does_not_leak_to_page(http_client):
    from tests.conftest import login_as
    import webapp.main as main
    from printaudit.ad.client import ADError
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_ad_client, get_auth_availability_dep

    login_as(http_client, role="superadmin")

    class _FailingSearch:
        def search_users(self, query, limit=25):
            raise ADError(f"bind failed: {SENSITIVE_DETAIL}")

    main.app.dependency_overrides[get_ad_client] = lambda: _FailingSearch()
    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(local_enabled=True, ad_enabled=True)

    resp = http_client.get("/admin/administrators", params={"q": "ivan"})
    assert resp.status_code == 200
    assert SENSITIVE_DETAIL not in resp.text


def test_admin_ad_group_search_error_does_not_leak_to_page(http_client):
    from tests.conftest import login_as
    import webapp.main as main
    from printaudit.ad.client import ADError
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_ad_client, get_auth_availability_dep

    login_as(http_client, role="admin")

    class _FailingSearch:
        def search_groups(self, query, limit=25):
            raise ADError(f"bind failed: {SENSITIVE_DETAIL}")

    main.app.dependency_overrides[get_ad_client] = lambda: _FailingSearch()
    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(local_enabled=True, ad_enabled=True)

    resp = http_client.get("/admin/ad-groups", params={"q": "acc"})
    assert resp.status_code == 200
    assert SENSITIVE_DETAIL not in resp.text


def test_group_members_sync_error_does_not_leak_to_redirect(http_client):
    from tests.conftest import login_as
    import webapp.main as main
    from printaudit.ad.client import ADError
    from printaudit.database import SessionLocal
    from printaudit.models import AdGroup
    from printaudit.ad_settings import AuthAvailability
    from webapp.deps import get_ad_client, get_auth_availability_dep

    login_as(http_client, role="admin")

    session = SessionLocal()
    group = AdGroup(dn="cn=accounting,dc=example,dc=local", sam_account_name="accounting", display_name="Accounting")
    session.add(group)
    session.commit()
    group_id = group.id
    session.close()

    class _FailingMembers:
        def get_group_members(self, group_dn):
            raise ADError(f"bind failed: {SENSITIVE_DETAIL}")

    main.app.dependency_overrides[get_ad_client] = lambda: _FailingMembers()
    main.app.dependency_overrides[get_auth_availability_dep] = lambda: AuthAvailability(local_enabled=True, ad_enabled=True)

    http_client.get("/admin/ad-groups")
    csrf = http_client.cookies.get("pa_csrf")
    resp = http_client.post(
        f"/admin/ad-groups/{group_id}/sync-members", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert SENSITIVE_DETAIL not in resp.headers["location"]


def test_printer_discovery_error_does_not_leak_to_redirect(http_client, monkeypatch):
    from tests.conftest import login_as

    login_as(http_client, role="admin")

    import webapp.admin_routes as admin_routes
    from printaudit.printers.discovery import PrinterDiscoveryError

    def _fail(db, fetch_fn=None):
        raise PrinterDiscoveryError(f"Export-Printers.ps1 failed: {SENSITIVE_DETAIL}")

    monkeypatch.setattr(admin_routes, "sync_printer_queues", _fail)

    http_client.get("/admin/printers")
    csrf = http_client.cookies.get("pa_csrf")
    resp = http_client.post("/admin/printers/discover", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert SENSITIVE_DETAIL not in resp.headers["location"]
