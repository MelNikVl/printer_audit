"""Тесты scripts/bootstrap_superadmin.py: не создаёт дефолтный пароль (его
не существует в принципе -- вход всегда через AD), требует явного
подтверждения при пропуске проверки AD, и отказывается создать второго
superadmin, если активный уже есть."""
from dataclasses import dataclass
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
    group_dns: List[str] = None


def test_skip_ad_check_creates_superadmin_without_password(app_env, monkeypatch, capsys):
    import scripts.bootstrap_superadmin as bootstrap

    monkeypatch.setattr(
        bootstrap.sys, "argv", ["bootstrap_superadmin.py", "--login", "DOMAIN\\ivanov", "--skip-ad-check"]
    )
    rc = bootstrap.main()
    assert rc == 0

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        user = session.query(AppUser).filter_by(login_normalized="domain\\ivanov").one()
        assert user.role == "superadmin"
        assert user.is_active is True
    finally:
        session.close()

    # Убедиться, что нигде не появляется понятие "пароль" -- вход только через AD.
    assert "пароль" not in capsys.readouterr().out.lower()


def test_bootstrap_without_ad_configured_and_without_skip_fails(app_env, monkeypatch):
    import scripts.bootstrap_superadmin as bootstrap

    monkeypatch.delenv("AD_SERVER", raising=False)
    monkeypatch.delenv("AD_BASE_DN", raising=False)
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_superadmin.py", "--login", "DOMAIN\\ivanov"])

    rc = bootstrap.main()
    assert rc == 1

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.query(AppUser).count() == 0
    finally:
        session.close()


def test_bootstrap_verifies_login_against_ad_when_configured(app_env, monkeypatch):
    import scripts.bootstrap_superadmin as bootstrap

    monkeypatch.setenv("AD_SERVER", "dc01.example.local")
    monkeypatch.setenv("AD_BASE_DN", "dc=example,dc=local")

    class _FakeADClient:
        def __init__(self, settings):
            pass

        def get_user_by_login(self, login):
            return _FakePrincipal(login_normalized="example.local\\ivanov", sam_account_name="ivanov")

    monkeypatch.setattr(bootstrap, "ADClient", _FakeADClient)
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_superadmin.py", "--login", "ivanov"])

    rc = bootstrap.main()
    assert rc == 0

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        user = session.query(AppUser).filter_by(login_normalized="example.local\\ivanov").one()
        assert user.role == "superadmin"
        assert user.display_name == "Ivan Ivanov"
        assert user.ad_sid == "S-1-5-21-1-2-3-1001"
    finally:
        session.close()


def test_bootstrap_fails_when_ad_user_not_found(app_env, monkeypatch):
    import scripts.bootstrap_superadmin as bootstrap

    monkeypatch.setenv("AD_SERVER", "dc01.example.local")
    monkeypatch.setenv("AD_BASE_DN", "dc=example,dc=local")

    class _FakeADClientNoResult:
        def __init__(self, settings):
            pass

        def get_user_by_login(self, login):
            return None

    monkeypatch.setattr(bootstrap, "ADClient", _FakeADClientNoResult)
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_superadmin.py", "--login", "ghost"])

    rc = bootstrap.main()
    assert rc == 1

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.query(AppUser).count() == 0
    finally:
        session.close()


def test_bootstrap_refuses_second_superadmin(app_env, monkeypatch):
    import scripts.bootstrap_superadmin as bootstrap
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    session.add(AppUser(login_normalized="domain\\existing", role="superadmin", is_active=True))
    session.commit()
    session.close()

    monkeypatch.setattr(
        bootstrap.sys, "argv", ["bootstrap_superadmin.py", "--login", "DOMAIN\\second", "--skip-ad-check"]
    )
    rc = bootstrap.main()
    assert rc == 1

    session = SessionLocal()
    try:
        assert session.query(AppUser).count() == 1
    finally:
        session.close()
