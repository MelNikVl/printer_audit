"""Гарантии безопасности назначения ролей, проверенные через реальные HTTP
роуты /admin/administrators/* (не только сервисный слой напрямую — см.
tests/test_admin_users.py — но и то, что роут действительно отдаёт понятную
ошибку 303->err=..., а не 500, и правило реально не применяется)."""


def _create_app_user(login, role, is_active=True):
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        user = AppUser(login_normalized=login, role=role, is_active=is_active)
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


# Примечание про "нельзя понизить/отключить/удалить ПОСЛЕДНЕГО активного
# superadmin" на уровне HTTP: /admin/administrators/* сам требует, чтобы
# actor уже был активным superadmin -- то есть actor всегда учитывается в
# числе "активных superadmin" в момент запроса. Поэтому сценарий "другой,
# не суицидальный actor понижает/отключает/удаляет ЕДИНСТВЕННОГO активного
# superadmin" физически недостижим через реальный HTTP-флоу: если actor тоже
# активный superadmin, целевой пользователь по определению не последний (сам
# actor остаётся); если actor не superadmin, роут его не пропустит (см.
# test_admin_cannot_reach_administrators_assign_endpoint_directly в
# test_auth_roles.py). Сам инвариант (что будет, если ЭТУ проверку всё же
# обойти или вызвать сервисный слой напрямую) проверен изолированно в
# tests/test_admin_users.py. Здесь же проверяем то, что РЕАЛЬНО достижимо
# через HTTP: правило "нельзя действие над самим собой", которое и защищает
# единственного superadmin от случайного самоотключения.


def test_http_cannot_disable_self_even_as_sole_superadmin(http_client):
    from tests.conftest import login_as

    self_id = login_as(http_client, role="superadmin", login="domain\\onlysuper")
    http_client.get("/admin/administrators")
    csrf = http_client.cookies.get("pa_csrf")

    resp = http_client.post(
        f"/admin/administrators/{self_id}/disable",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.get(AppUser, self_id).is_active is True
    finally:
        session.close()


def test_http_cannot_delete_self_even_as_sole_superadmin(http_client):
    from tests.conftest import login_as

    self_id = login_as(http_client, role="superadmin", login="domain\\onlysuper")
    http_client.get("/admin/administrators")
    csrf = http_client.cookies.get("pa_csrf")

    resp = http_client.post(
        f"/admin/administrators/{self_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.get(AppUser, self_id) is not None
    finally:
        session.close()


def test_http_cannot_change_own_role(http_client):
    from tests.conftest import login_as

    login_as(http_client, role="superadmin", login="domain\\self")
    http_client.get("/admin/administrators")
    csrf = http_client.cookies.get("pa_csrf")

    resp = http_client.post(
        "/admin/administrators/assign",
        data={"csrf_token": csrf, "login": "domain\\self", "role": "viewer"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.query(AppUser).filter_by(login_normalized="domain\\self").one().role == "superadmin"
    finally:
        session.close()


def test_http_superadmin_can_demote_when_another_active_superadmin_remains(http_client):
    from tests.conftest import login_as

    target_id = _create_app_user("domain\\other_super", "superadmin")
    login_as(http_client, role="superadmin", login="domain\\actor")
    http_client.get("/admin/administrators")
    csrf = http_client.cookies.get("pa_csrf")

    resp = http_client.post(
        "/admin/administrators/assign",
        data={"csrf_token": csrf, "login": "domain\\other_super", "role": "viewer"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.get(AppUser, target_id).role == "viewer"
    finally:
        session.close()
