"""Общие FastAPI-зависимости: БД-сессия, текущий пользователь, проверка
роли, CSRF. Используются и отчётами, и /admin/*, и /api/* — без исключений:
всё, что не относится к /login и статике, обязано пройти хотя бы require_login.
"""
import re
from typing import Optional

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from printaudit.ad.client import ADClient
from printaudit.ad_settings import get_ad_settings
from printaudit.database import SessionLocal
from printaudit.models import AppUser
from printaudit.security.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME, csrf_tokens_match
from printaudit.security.sessions import SESSION_COOKIE_NAME, get_app_user_for_token
from webapp.errors import Forbidden, NotAuthenticated


def get_ad_client() -> ADClient:
    """Отдельная FastAPI-зависимость (не создание ADClient прямо в роуте),
    чтобы тесты могли подменить её через app.dependency_overrides и
    прогнать вход/поиск через mock LDAP вместо реального AD."""
    return ADClient(get_ad_settings())


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return ""


_LEADING_CONTROL_OR_SPACE_RE = re.compile(r"^[\x00-\x20]+")


def safe_next_path(raw: Optional[str]) -> str:
    """Защита от open redirect в ?next=/POST-параметре next формы логина.

    Разрешён ТОЛЬКО локальный абсолютный путь: начинается ровно с одного '/'
    и не продолжается вторым '/' (в т.ч. после пробельных/управляющих
    символов, которые браузер может проигнорировать) — что отсекает:
      - протокол-относительные ссылки ("//evil.com" — браузер трактует как
        тот же протокол + чужой хост);
      - обратные слэши ("/\\evil.com", "\\\\evil.com" — браузеры трактуют
        "\\" как "/" в URL, так что это тот же protocol-relative трюк в
        другой записи);
      - абсолютные URL с чужой схемой/хостом ("http://evil.com",
        "javascript:...") — они не начинаются с "/" вообще.

    Любое значение, не прошедшее проверку (включая пустое/отсутствующее),
    заменяется на "/".
    """
    if not raw:
        return "/"
    candidate = raw.strip().replace("\\", "/")
    if not candidate or candidate[0] != "/":
        return "/"
    rest = _LEADING_CONTROL_OR_SPACE_RE.sub("", candidate[1:])
    if rest.startswith("/"):
        return "/"
    return candidate


def csrf_token(request: Request) -> str:
    """Значение CSRF-токена для текущего запроса — выставляется
    CsrfCookieMiddleware (webapp/middleware.py) в request.state ДО того, как
    выполнится сам роут, поэтому доступно и на самой первой странице без
    cookie у клиента (сама cookie будет выставлена на ответ этим же
    middleware). Использовать в шаблонах для скрытого поля формы."""
    return request.state.csrf_token


def get_current_app_user_optional(
    request: Request, db: Session = Depends(get_db)
) -> Optional[AppUser]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return get_app_user_for_token(db, token)


def require_login(
    request: Request, db: Session = Depends(get_db)
) -> AppUser:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_app_user_for_token(db, token)
    if user is None:
        raise NotAuthenticated(next_path=request.url.path)
    if not user.is_active:
        raise Forbidden("Ваш доступ отключён администратором. Обратитесь к администратору системы.")
    request.state.app_user = user
    return user


def require_role(*allowed_roles: str):
    """Фабрика зависимости: доступ только для перечисленных ролей.
    Всегда проверяется ПОСЛЕ require_login (то есть неавторизованный
    получит редирект на /login, а не 403 -- 403 значит "вошёл, но не хватает
    прав", что честнее по отношению к пользователю и не выдаёт наличие
    раздела тому, кто даже не залогинен)."""

    def _dependency(user: AppUser = Depends(require_login)) -> AppUser:
        if user.role not in allowed_roles:
            raise Forbidden("Недостаточно прав для этого раздела.")
        return user

    return _dependency


async def require_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        submitted = request.headers.get(CSRF_HEADER_NAME)
    else:
        form = await request.form()
        submitted = form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER_NAME)
    if not csrf_tokens_match(cookie_token, submitted):
        raise Forbidden("Неверный или отсутствующий CSRF-токен. Обновите страницу и повторите действие.")
