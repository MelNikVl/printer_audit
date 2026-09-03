"""Тесты /admin/sites и /admin/print-servers: RBAC, регистрация с
одноразовым показом токена, ротация (старый токен сразу невалиден),
включение/отключение, и что список отражает вычисляемый статус и метрики."""
from tests.conftest import login_as


def _csrf(http_client, url):
    http_client.get(url)
    return http_client.cookies.get("pa_csrf")


def test_admin_sites_and_print_servers_forbidden_for_viewer(http_client):
    login_as(http_client, role="viewer")
    assert http_client.get("/admin/sites").status_code == 403
    assert http_client.get("/admin/print-servers").status_code == 403


def test_create_site_then_register_print_server_shows_token_once(http_client):
    login_as(http_client, role="admin")

    csrf = _csrf(http_client, "/admin/sites")
    resp = http_client.post(
        "/admin/sites/create",
        data={"csrf_token": csrf, "site_code": "BRANCH-2", "name": "Филиал 2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]

    from printaudit.database import SessionLocal
    from printaudit.models import Site

    session = SessionLocal()
    site = session.query(Site).filter_by(site_code="BRANCH-2").one()
    site_id = site.id
    session.close()

    csrf = _csrf(http_client, "/admin/print-servers")
    resp = http_client.post(
        "/admin/print-servers/create",
        data={"csrf_token": csrf, "site_id": site_id, "server_name": "PRINTSRV-2A"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "msg=" in location
    # Токен показан РОВНО в этом редиректе (одноразово).
    import urllib.parse

    msg = urllib.parse.unquote_plus(location.split("msg=", 1)[1])
    assert "Токен агента" in msg

    session = SessionLocal()
    from printaudit.models import PrintServer

    server = session.query(PrintServer).filter_by(server_name="PRINTSRV-2A").one()
    assert server.token_hash is not None
    assert server.token_hash not in msg  # хэш не совпадает с текстом сырого токена
    session.close()

    page = http_client.get("/admin/print-servers")
    assert "PRINTSRV-2A" in page.text
    assert "pending" in page.text  # ещё не было heartbeat


def test_duplicate_server_name_on_same_site_rejected(http_client):
    login_as(http_client, role="admin")
    from printaudit.database import SessionLocal
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "DUPTEST", name="Dup Test")
    session.commit()
    site_id = site.id
    session.close()

    csrf = _csrf(http_client, "/admin/print-servers")
    http_client.post(
        "/admin/print-servers/create",
        data={"csrf_token": csrf, "site_id": site_id, "server_name": "SAME-NAME"},
        follow_redirects=False,
    )
    resp = http_client.post(
        "/admin/print-servers/create",
        data={"csrf_token": csrf, "site_id": site_id, "server_name": "SAME-NAME"},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_rotate_token_invalidates_old_token(http_client, monkeypatch):
    login_as(http_client, role="admin")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")

    from printaudit.database import SessionLocal
    from printaudit.security.agent_tokens import hash_agent_token
    from printaudit.sites import get_or_create_print_server, get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "ROTTEST", name="Rotate Test")
    server = get_or_create_print_server(session, site, "ROT-1")
    old_token = "old-raw-token-for-test"
    server.token_hash = hash_agent_token(old_token)
    session.commit()
    server_id = server.id
    site_uuid = site.uuid
    server_uuid = server.uuid
    session.close()

    batch = {
        "protocol_version": 1, "site_uuid": site_uuid, "print_server_uuid": server_uuid,
        "generated_at": "2026-09-03T10:00:00+00:00", "events": [],
    }
    resp = http_client.post(
        "/api/v1/agent/events/batch", json=batch, headers={"Authorization": f"Bearer {old_token}"}
    )
    assert resp.status_code == 200

    csrf = _csrf(http_client, "/admin/print-servers")
    http_client.post(
        f"/admin/print-servers/{server_id}/rotate-token",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    resp2 = http_client.post(
        "/api/v1/agent/events/batch", json=batch, headers={"Authorization": f"Bearer {old_token}"}
    )
    assert resp2.status_code == 401


def test_disable_and_enable_print_server(http_client, monkeypatch):
    login_as(http_client, role="admin")
    monkeypatch.setenv("AGENT_REQUIRE_HTTPS", "false")

    from printaudit.database import SessionLocal
    from printaudit.security.agent_tokens import generate_agent_token, hash_agent_token
    from printaudit.sites import get_or_create_print_server, get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "DISTEST", name="Disable Test")
    server = get_or_create_print_server(session, site, "DIS-1")
    token = generate_agent_token()
    server.token_hash = hash_agent_token(token)
    session.commit()
    server_id = server.id
    site_uuid = site.uuid
    server_uuid = server.uuid
    session.close()

    csrf = _csrf(http_client, "/admin/print-servers")
    http_client.post(f"/admin/print-servers/{server_id}/disable", data={"csrf_token": csrf}, follow_redirects=False)

    batch = {
        "protocol_version": 1, "site_uuid": site_uuid, "print_server_uuid": server_uuid,
        "generated_at": "2026-09-03T10:00:00+00:00", "events": [],
    }
    resp = http_client.post("/api/v1/agent/events/batch", json=batch, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401

    http_client.post(f"/admin/print-servers/{server_id}/enable", data={"csrf_token": csrf}, follow_redirects=False)
    resp2 = http_client.post("/api/v1/agent/events/batch", json=batch, headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200
