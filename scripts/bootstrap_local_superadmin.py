"""Безопасный bootstrap первого ЛОКАЛЬНОГО superadmin (auth_provider="local").

Использование (только логин аргументом — пароль вводится интерактивно):

    python scripts\\bootstrap_local_superadmin.py --login localadmin

Пароль запрашивается дважды через getpass (не отображается на экране,
не принимается как аргумент командной строки — так намеренно: пароль в
argv виден в истории шелла и в списке процессов (`ps`/Task Manager) любому
другому пользователю той же машины, что сводит на нет весь смысл его
секретности).

Как и scripts/bootstrap_superadmin.py (AD-вариант), отказывается создавать
ВТОРОГО активного superadmin — неважно, локального или AD: если такой уже
есть, добавляйте остальных через /admin/administrators, войдя под ним."""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.ad_normalize import normalize_login  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import AppUser  # noqa: E402
from printaudit.roles import SUPERADMIN  # noqa: E402
from printaudit.security.passwords import WeakPasswordError, hash_password, validate_password_strength  # noqa: E402
from printaudit.timeutil import utcnow  # noqa: E402


def _read_password_twice() -> str:
    while True:
        password = getpass.getpass("Пароль для нового локального superadmin: ")
        try:
            validate_password_strength(password)
        except WeakPasswordError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            continue
        confirm = getpass.getpass("Повторите пароль: ")
        if password != confirm:
            print("Пароли не совпадают, попробуйте снова.", file=sys.stderr)
            continue
        return password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", required=True, help="Логин нового локального superadmin (без пробелов).")
    args = parser.parse_args()

    login = args.login.strip()
    if not login:
        print("Логин не может быть пустым.", file=sys.stderr)
        return 1
    login_normalized = normalize_login(login)

    session = SessionLocal()
    try:
        existing_superadmin = session.query(AppUser).filter_by(role=SUPERADMIN, is_active=True).first()
        if existing_superadmin is not None:
            print(
                f"Уже есть активный superadmin: {existing_superadmin.login_normalized}. "
                f"Добавляйте остальных через /admin/administrators, войдя под ним.",
                file=sys.stderr,
            )
            return 1

        if session.query(AppUser).filter_by(login_normalized=login_normalized).first():
            print(f"Пользователь с логином «{login_normalized}» уже существует.", file=sys.stderr)
            return 1

        password = _read_password_twice()

        app_user = AppUser(
            login_normalized=login_normalized,
            role=SUPERADMIN,
            is_active=True,
            auth_provider="local",
            password_hash=hash_password(password),
            must_change_password=False,  # админ только что сам выбрал пароль осознанно
            password_changed_at=utcnow(),
            assigned_by_id=None,  # bootstrap, не назначен другим пользователем
            assigned_at=utcnow(),
        )
        session.add(app_user)
        session.commit()
        # password выходит из области видимости здесь и не логируется нигде выше.
        print(f"Готово: {login_normalized} создан как локальный superadmin. Войдите через веб-интерфейс.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
