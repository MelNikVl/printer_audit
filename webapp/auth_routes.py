"""Вход/выход. Пароль AD передаётся в POST /login только для проверки через
ADClient.authenticate() -- никогда не логируется, не сохраняется в БД."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from printaudit.ad.client import ADAuthError, ADClient, ADError
from printaudit.ad_settings import get_session_settings
from printaudit.models import AppUser
from printaudit.security.sessions import SESSION_COOKIE_NAME, create_session, revoke_session
from webapp.deps import csrf_token, get_ad_client, get_client_ip, get_db, require_csrf
from webapp.templating import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "csrf_token": csrf_token(request), "next": next, "error": None},
    )


@router.post("/login", dependencies=[Depends(require_csrf)])
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    ad_client: ADClient = Depends(get_ad_client),
):
    def _error(message: str, status_code: int):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "csrf_token": csrf_token(request), "next": next, "error": message},
            status_code=status_code,
        )

    if not login.strip() or not password:
        return _error("Введите логин и пароль.", 400)

    try:
        principal = ad_client.authenticate(login, password)
    except ADAuthError:
        return _error("Неверный логин или пароль.", 401)
    except ADError as exc:
        return _error(f"Не удалось связаться с Active Directory: {exc}", 503)

    app_user = db.query(AppUser).filter_by(login_normalized=principal.login_normalized).first()
    if app_user is None and principal.sid:
        app_user = db.query(AppUser).filter_by(ad_sid=principal.sid).first()

    if app_user is None or not app_user.is_active:
        return _error(
            "Логин и пароль верны, но доступ к этой системе вам не выдан. "
            "Обратитесь к администратору, чтобы вас добавили в разделе «Администраторы».",
            403,
        )

    app_user.ad_sid = principal.sid or app_user.ad_sid
    app_user.ad_object_guid = principal.object_guid or app_user.ad_object_guid
    app_user.display_name = principal.display_name or app_user.display_name
    app_user.email = principal.email or app_user.email
    db.commit()

    token = create_session(
        db, app_user, ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent")
    )
    session_settings = get_session_settings()
    redirect = RedirectResponse(url=next or "/", status_code=303)
    redirect.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=session_settings.cookie_secure,
        max_age=session_settings.lifetime_minutes * 60,
    )
    return redirect


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    revoke_session(db, token)
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(SESSION_COOKIE_NAME)
    return redirect
