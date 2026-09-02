"""HTTP-уровень: без входа нельзя увидеть отчёты/API/CSV, роли разграничивают
доступ к /admin/*, прямой URL не помогает, CSRF обязателен для изменяющих
запросов. Использует tests/conftest.py::http_client + login_as (сессия
заводится напрямую в БД, без реального AD -- вход как таковой отдельно
проверяется в tests/test_ad_client.py и tests/test_login_flow.py)."""
import pytest


# ---------------------------------------------------------------------------
# Без входа
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/by-department", "/by-user", "/by-printer", "/export"],
)
def test_unauthenticated_html_pages_redirect_to_login(http_client, path):
    resp = http_client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_unauthenticated_export_csv_returns_401_json_not_a_download(http_client):
    resp = http_client.get("/export/csv")
    assert resp.status_code == 401
    assert resp.json()["detail"]


@pytest.mark.parametrize(
    "path",
    ["/api/print-jobs", "/api/stats/by-department", "/api/stats/by-user", "/api/stats/by-printer"],
)
def test_unauthenticated_api_returns_401_json(http_client, path):
    resp = http_client.get(path)
    assert resp.status_code == 401
    assert resp.json()["detail"]


def test_unauthenticated_admin_redirects_to_login(http_client):
    resp = http_client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# viewer: видит отчёты/экспорт, не видит /admin
# ---------------------------------------------------------------------------


def test_viewer_can_see_dashboard_and_export(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="viewer")
    assert http_client.get("/").status_code == 200
    assert http_client.get("/export/csv").status_code == 200
    assert http_client.get("/api/print-jobs").status_code == 200


def test_viewer_cannot_access_admin_overview(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="viewer")
    resp = http_client.get("/admin")
    assert resp.status_code == 403


def test_viewer_cannot_access_admin_departments_via_direct_url(http_client):
    """Прямой URL не должен давать доступ, даже если пункт меню скрыт."""
    from tests.conftest import login_as

    login_as(http_client, role="viewer")
    resp = http_client.get("/admin/departments")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# admin: видит большинство /admin/*, но не /admin/administrators
# ---------------------------------------------------------------------------


def test_admin_can_access_departments_and_printers(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="admin")
    assert http_client.get("/admin/departments").status_code == 200
    assert http_client.get("/admin/printers").status_code == 200
    assert http_client.get("/admin/pricing").status_code == 200
    assert http_client.get("/admin/ad-users").status_code == 200
    assert http_client.get("/admin/ad-groups").status_code == 200


def test_admin_cannot_access_administrators_page(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="admin")
    resp = http_client.get("/admin/administrators")
    assert resp.status_code == 403


def test_admin_cannot_reach_administrators_assign_endpoint_directly(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="admin")
    csrf = http_client.cookies.get("pa_csrf")
    resp = http_client.post(
        "/admin/administrators/assign",
        data={"csrf_token": csrf, "login": "domain\\newsuper", "role": "superadmin"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# superadmin: видит всё
# ---------------------------------------------------------------------------


def test_superadmin_can_access_administrators_page(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="superadmin")
    assert http_client.get("/admin/administrators").status_code == 200


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_post_without_csrf_token_is_rejected(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="admin")
    resp = http_client.post("/admin/departments/create", data={"name": "Без CSRF"})
    assert resp.status_code == 403

    from printaudit.database import SessionLocal
    from printaudit.models import Department

    session = SessionLocal()
    try:
        assert session.query(Department).filter_by(name="Без CSRF").count() == 0
    finally:
        session.close()


def test_post_with_wrong_csrf_token_is_rejected(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="admin")
    http_client.get("/admin/departments")  # получить настоящую csrf cookie
    resp = http_client.post(
        "/admin/departments/create", data={"name": "Неверный CSRF", "csrf_token": "garbage-value"}
    )
    assert resp.status_code == 403


def test_post_with_matching_csrf_token_succeeds(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="admin")
    http_client.get("/admin/departments")
    csrf = http_client.cookies.get("pa_csrf")
    resp = http_client.post(
        "/admin/departments/create", data={"name": "Отдел с CSRF", "csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    from printaudit.database import SessionLocal
    from printaudit.models import Department

    session = SessionLocal()
    try:
        assert session.query(Department).filter_by(name="Отдел с CSRF").count() == 1
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Отключённый пользователь
# ---------------------------------------------------------------------------


def test_disabled_app_user_session_is_rejected_with_403_not_redirect(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="viewer", is_active=False)
    resp = http_client.get("/")
    assert resp.status_code == 403
