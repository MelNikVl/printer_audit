"""scripts/bootstrap_local_superadmin.py: пароль только через getpass (дважды,
не через argv), безопасное создание первого локального superadmin, отказ
создать второго."""
import pytest


def test_cli_rejects_password_as_command_line_argument(app_env, monkeypatch):
    """Требование: пароль ЗАПРЕЩЕНО принимать аргументом командной строки.
    Проверяем НАСТОЯЩИЙ парсер скрипта (не копию) -- --password должен быть
    для него нераспознанным аргументом и argparse должен завершить процесс
    с ошибкой, а не тихо принять и использовать значение."""
    import scripts.bootstrap_local_superadmin as bootstrap

    monkeypatch.setattr(
        bootstrap.sys, "argv",
        ["bootstrap_local_superadmin.py", "--login", "localadmin", "--password", "SomePassword123"],
    )
    with pytest.raises(SystemExit):
        bootstrap.main()

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.query(AppUser).count() == 0  # ничего не создалось
    finally:
        session.close()


def test_bootstrap_creates_local_superadmin(app_env, monkeypatch):
    import scripts.bootstrap_local_superadmin as bootstrap

    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_local_superadmin.py", "--login", "localadmin"])
    passwords = iter(["CorrectHorseBattery1", "CorrectHorseBattery1"])
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a, **k: next(passwords))

    rc = bootstrap.main()
    assert rc == 0

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.passwords import verify_password

    session = SessionLocal()
    try:
        user = session.query(AppUser).filter_by(login_normalized="localadmin").one()
        assert user.role == "superadmin"
        assert user.auth_provider == "local"
        assert user.must_change_password is False
        assert verify_password("CorrectHorseBattery1", user.password_hash)
    finally:
        session.close()


def test_bootstrap_retries_on_password_mismatch(app_env, monkeypatch):
    import scripts.bootstrap_local_superadmin as bootstrap

    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_local_superadmin.py", "--login", "localadmin"])
    # первая попытка: пароли не совпадают -> должен переспросить
    passwords = iter(["CorrectHorseBattery1", "Mismatch12345", "CorrectHorseBattery1", "CorrectHorseBattery1"])
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a, **k: next(passwords))

    rc = bootstrap.main()
    assert rc == 0

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser
    from printaudit.security.passwords import verify_password

    session = SessionLocal()
    try:
        user = session.query(AppUser).filter_by(login_normalized="localadmin").one()
        assert verify_password("CorrectHorseBattery1", user.password_hash)
    finally:
        session.close()


def test_bootstrap_rejects_weak_password_and_retries(app_env, monkeypatch):
    import scripts.bootstrap_local_superadmin as bootstrap

    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_local_superadmin.py", "--login", "localadmin"])
    passwords = iter(["short", "CorrectHorseBattery1", "CorrectHorseBattery1"])
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a, **k: next(passwords))

    rc = bootstrap.main()
    assert rc == 0

    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    try:
        assert session.query(AppUser).filter_by(login_normalized="localadmin").count() == 1
    finally:
        session.close()


def test_bootstrap_refuses_second_superadmin(app_env, monkeypatch):
    import scripts.bootstrap_local_superadmin as bootstrap
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    session.add(AppUser(login_normalized="existing", role="superadmin", is_active=True, auth_provider="ad"))
    session.commit()
    session.close()

    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_local_superadmin.py", "--login", "localadmin"])
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a, **k: "CorrectHorseBattery1")

    rc = bootstrap.main()
    assert rc == 1

    session = SessionLocal()
    try:
        assert session.query(AppUser).count() == 1
    finally:
        session.close()


def test_bootstrap_refuses_duplicate_login(app_env, monkeypatch):
    import scripts.bootstrap_local_superadmin as bootstrap
    from printaudit.database import SessionLocal
    from printaudit.models import AppUser

    session = SessionLocal()
    session.add(AppUser(login_normalized="localadmin", role="viewer", is_active=True, auth_provider="local"))
    session.commit()
    session.close()

    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_local_superadmin.py", "--login", "localadmin"])
    rc = bootstrap.main()
    assert rc == 1


def test_bootstrap_password_never_printed(app_env, monkeypatch, capsys):
    import scripts.bootstrap_local_superadmin as bootstrap

    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap_local_superadmin.py", "--login", "localadmin"])
    passwords = iter(["CorrectHorseBattery1", "CorrectHorseBattery1"])
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a, **k: next(passwords))

    bootstrap.main()
    out = capsys.readouterr().out
    assert "CorrectHorseBattery1" not in out
