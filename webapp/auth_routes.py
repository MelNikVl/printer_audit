"""Вход/выход, два независимых провайдера: local и ad.

Пароль (локальный или AD) передаётся в POST /login только для проверки --
никогда не логируется, не сохраняется в БД (для AD -- используется один раз
для bind и выбрасывается; для local -- см. printaudit.security.passwords,
хранится только Argon2id-хэш). Когда AD_AUTH_ENABLED=false (или AD не
настроен), код НИГДЕ в этом файле не создаёт ADClient и не делает сетевых
вызовов к LDAP -- see auth_availability.ad_enabled guards below."""
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from printaudit.ad.client import ADAuthError, ADClient, ADError
from printaudit.ad_settings import AuthAvailability, get_session_settings
from printaudit.models import AppUser
from printaudit.security.local_auth import (
    LocalAuthInvalidCredentialsError,
    LocalAuthLockedError,
    authenticate_local,
)
from printaudit.security.password_management import PasswordChangeError, change_password
from printaudit.security.passwords import MIN_PASSWORD_LENGTH
from printaudit.security.sessions import SESSION_COOKIE_NAME, create_session, revoke_session
from webapp.deps import (
    csrf_token,
    get_ad_client,
    get_auth_availability_dep,
    get_client_ip,
    get_db,
    require_csrf,
    require_login,
    safe_next_path,
)
from webapp.errors import safe_error_message
from webapp.templating import templates

router = APIRouter()


@router.get("/login")
def login_page(
    request: Request,
    next: str = "/",
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request, "csrf_token": csrf_token(request), "next": safe_next_path(next),
            "error": None, "auth": auth_availability,
        },
    )


@router.post("/login", dependencies=[Depends(require_csrf)])
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    provider: Literal["local", "ad"] = Form("local"),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    ad_client: ADClient = Depends(get_ad_client),
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    safe_next = safe_next_path(next)

    def _error(message: str, status_code: int):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, "csrf_token": csrf_token(request), "next": safe_next,
                "error": message, "auth": auth_availability, "provider": provider,
            },
            status_code=status_code,
        )

    if not login.strip() or not password:
        return _error("Введите логин и пароль.", 400)

    if provider == "local":
        if not auth_availability.local_enabled:
            return _error("Локальный вход отключён администратором.", 403)
        app_user = _authenticate_local_provider(db, login, password)
        if isinstance(app_user, str):  # сообщение об ошибке
            return _error(app_user, 401)
    else:
        if not auth_availability.ad_enabled:
            # Важно: ветка AD-логина не выполняется вообще, если AD выключен —
            # ни одного обращения к LDAP отсюда быть не должно.
            return _error("Вход через Active Directory отключён администратором.", 403)
        app_user = _authenticate_ad_provider(db, ad_client, login, password)
        if isinstance(app_user, tuple):  # (сообщение, статус)
            return _error(app_user[0], app_user[1])

    token = create_session(
        db, app_user, ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent")
    )
    session_settings = get_session_settings()
    redirect = RedirectResponse(url=safe_next, status_code=303)
    redirect.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=session_settings.cookie_secure,
        max_age=session_settings.lifetime_minutes * 60,
    )
    return redirect


def _authenticate_local_provider(db: Session, login: str, password: str):
    """Возвращает AppUser при успехе, либо str с сообщением об ошибке."""
    try:
        return authenticate_local(db, login, password)
    except LocalAuthLockedError:
        return (
            "Учётная запись временно заблокирована после нескольких неверных "
            "попыток входа. Попробуйте позже."
        )
    except LocalAuthInvalidCredentialsError:
        return "Неверный логин или пароль."


def _authenticate_ad_provider(db: Session, ad_client: ADClient, login: str, password: str):
    """Возвращает AppUser при успехе, либо (сообщение, статус_код) при ошибке."""
    try:
        principal = ad_client.authenticate(login, password)
    except ADAuthError:
        return ("Неверный логин или пароль.", 401)
    except ADError as exc:
        return (safe_error_message(exc, "вход через AD"), 503)

    app_user = db.query(AppUser).filter_by(login_normalized=principal.login_normalized).first()
    if app_user is None and principal.sid:
        app_user = db.query(AppUser).filter_by(ad_sid=principal.sid).first()

    if app_user is None or not app_user.is_active:
        return (
            "Логин и пароль верны, но доступ к этой системе вам не выдан. "
            "Обратитесь к администратору, чтобы вас добавили в разделе «Администраторы».",
            403,
        )

    app_user.ad_sid = principal.sid or app_user.ad_sid
    app_user.ad_object_guid = principal.object_guid or app_user.ad_object_guid
    app_user.display_name = principal.display_name or app_user.display_name
    app_user.email = principal.email or app_user.email
    db.commit()
    return app_user


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    revoke_session(db, token)
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(SESSION_COOKIE_NAME)
    return redirect


# ---------------------------------------------------------------------------
# Смена пароля (только auth_provider="local")
# ---------------------------------------------------------------------------


@router.get("/change-password")
def change_password_page(
    request: Request,
    current_user: AppUser = Depends(require_login),
):
    return templates.TemplateResponse(
        "change_password.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "error": None, "forced": current_user.must_change_password,
            "min_length": MIN_PASSWORD_LENGTH,
        },
    )


@router.post("/change-password", dependencies=[Depends(require_csrf)])
def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    def _error(message: str):
        return templates.TemplateResponse(
            "change_password.html",
            {
                "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
                "error": message, "forced": current_user.must_change_password,
                "min_length": MIN_PASSWORD_LENGTH,
            },
            status_code=400,
        )

    if new_password != new_password_confirm:
        return _error("Новый пароль и подтверждение не совпадают.")

    try:
        change_password(
            db, user=current_user, current_password=current_password, new_password=new_password,
            ip_address=get_client_ip(request),
        )
    except PasswordChangeError as exc:
        return _error(str(exc))

    # Смена пароля отзывает ВСЕ сессии, включая текущую (см.
    # printaudit.security.password_management) -- отправляем на /login,
    # cookie этой сессии всё равно уже недействительна на сервере.
    # Кириллица percent-encoded через urlencode -- Location это HTTP-заголовок,
    # сырой UTF-8 в нём недопустим.
    query = urlencode({"msg": "Пароль изменён. Войдите заново"})
    redirect = RedirectResponse(url=f"/login?{query}", status_code=303)
    redirect.delete_cookie(SESSION_COOKIE_NAME)
    return redirect
