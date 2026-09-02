"""Безопасный bootstrap первого superadmin. Никакого стандартного пароля не
создаёт и не может создать — вход всё равно проверяется через AD при
логине; эта команда только ВЫДАЁТ доступ конкретному AD-логину.

Использование (интерактивно, спросит логин):

    python scripts\\bootstrap_superadmin.py

Либо неинтерактивно, через переменную окружения (удобно для скриптов
установки/CI, но не обязательно — это НЕ пароль, а просто логин):

    set BOOTSTRAP_SUPERADMIN_LOGIN=EXAMPLE\\ivanov
    python scripts\\bootstrap_superadmin.py

По умолчанию требует, чтобы указанный логин НАШЁЛСЯ в AD (через сервисный
bind-аккаунт AD_BIND_USER/AD_BIND_PASSWORD) — так исключается опечатка,
дающая superadmin несуществующему аккаунту. Если AD пока не настроен (только
для первого запуска в изолированном тестовом стенде), это можно обойти
флагом --skip-ad-check, но тогда логин НЕ проверяется вообще, и это явно
предупреждается в выводе.

Если superadmin уже есть — команда откажется создавать второго молча;
используйте /admin/administrators в веб-интерфейсе, залогинившись под
существующим superadmin, для добавления остальных.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.ad.client import ADClient, ADError  # noqa: E402
from printaudit.ad_normalize import normalize_login  # noqa: E402
from printaudit.ad_settings import get_ad_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import AppUser  # noqa: E402
from printaudit.roles import SUPERADMIN  # noqa: E402
from printaudit.timeutil import utcnow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--login", default=os.environ.get("BOOTSTRAP_SUPERADMIN_LOGIN"),
        help="Логин AD в любом формате (DOMAIN\\login / login@domain / login). "
             "По умолчанию берётся из BOOTSTRAP_SUPERADMIN_LOGIN.",
    )
    parser.add_argument(
        "--skip-ad-check", action="store_true",
        help="Не проверять логин через AD перед выдачей доступа (только для изолированных тестовых стендов).",
    )
    args = parser.parse_args()

    login = args.login
    if not login:
        login = input("Логин AD для первого superadmin (DOMAIN\\login): ").strip()
    if not login:
        print("Логин не указан.", file=sys.stderr)
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

        display_name = None
        email = None
        ad_sid = None
        ad_object_guid = None

        if args.skip_ad_check:
            print(
                "!!! --skip-ad-check: логин НЕ проверен через AD. Убедитесь, что он указан верно "
                "— опечатка выдаст superadmin несуществующему или чужому аккаунту.",
                file=sys.stderr,
            )
        else:
            ad_settings = get_ad_settings()
            if not ad_settings.is_configured:
                print(
                    "AD не настроен (AD_SERVER/AD_BASE_DN пусты в .env) — "
                    "либо настройте AD, либо явно укажите --skip-ad-check.",
                    file=sys.stderr,
                )
                return 1
            try:
                client = ADClient(ad_settings)
                principal = client.get_user_by_login(login_normalized)
            except ADError as exc:
                print(f"Не удалось проверить логин через AD: {exc}", file=sys.stderr)
                return 1
            if principal is None:
                print(f"Логин «{login_normalized}» не найден в AD.", file=sys.stderr)
                return 1
            display_name = principal.display_name
            email = principal.email
            ad_sid = principal.sid
            ad_object_guid = principal.object_guid
            login_normalized = principal.login_normalized

        app_user = AppUser(
            login_normalized=login_normalized,
            display_name=display_name,
            email=email,
            ad_sid=ad_sid,
            ad_object_guid=ad_object_guid,
            role=SUPERADMIN,
            is_active=True,
            assigned_by_id=None,  # bootstrap, не назначен другим пользователем
            assigned_at=utcnow(),
        )
        session.add(app_user)
        session.commit()
        print(f"Готово: {login_normalized} назначен superadmin. Войдите через веб-интерфейс с паролем AD.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
