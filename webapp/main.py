"""Веб-UI и JSON API для отчётов по печати + админка. Запуск (из корня репозитория):

    python -m uvicorn webapp.main:app --host 0.0.0.0 --port 8000

См. deploy/run_webapp.ps1 для готового скрипта запуска и docs/ADMIN_GUIDE.md
для варианта установки как службы Windows (NSSM).

Все страницы отчётов, /api/* и /export/csv требуют входа (require_login) --
без сессии доступен только /login. Раздел /admin дополнительно требует роль
admin/superadmin (см. webapp/admin_routes.py)."""
import csv
import io
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from printaudit import queries  # noqa: E402
from printaudit.ad_settings import validate_session_secret  # noqa: E402
from printaudit.config import get_settings  # noqa: E402
from printaudit.models import AppUser  # noqa: E402
from webapp import admin_routes, auth_routes  # noqa: E402
from webapp.deps import csrf_token, get_db, require_login  # noqa: E402
from webapp.errors import Forbidden, NotAuthenticated  # noqa: E402
from webapp.middleware import CsrfCookieMiddleware  # noqa: E402
from webapp.templating import BASE_DIR, templates  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fail closed: без настоящего SESSION_SECRET_KEY сервер вообще не
    # поднимается (не "поднимается с предупреждением в лог", которое никто
    # не читает) -- см. printaudit/ad_settings.py::validate_session_secret
    # для точных условий (не задан / плейсхолдер CHANGE_ME / совпадает с
    # dev-заглушкой из исходников / короче 32 символов). Намеренно НЕ в
    # printaudit.ad_settings.get_session_settings() -- она нужна и
    # collector'у/CLI-скриптам, которым веб-сессии не нужны вообще, и им не
    # следует падать из-за отсутствия этой переменной.
    validate_session_secret(os.environ.get("SESSION_SECRET_KEY"))
    yield


app = FastAPI(title="Print Audit", lifespan=lifespan)
app.add_middleware(CsrfCookieMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(auth_routes.router)
app.include_router(admin_routes.router)


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/") or path.startswith("/export/csv")


@app.exception_handler(NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: NotAuthenticated):
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=401, content={"detail": "Требуется вход в систему"})
    return RedirectResponse(url=f"/login?next={exc.next_path}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden_handler(request: Request, exc: Forbidden):
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=403, content={"detail": exc.message})
    return templates.TemplateResponse(
        "403.html", {"request": request, "message": exc.message}, status_code=403
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and not _is_api_path(request.url.path):
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return templates.TemplateResponse(
        "403.html", {"request": request, "message": str(exc.detail)}, status_code=exc.status_code
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    return templates.TemplateResponse(
        "403.html", {"request": request, "message": "Некорректный запрос."}, status_code=400
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    # Никогда не показываем traceback/детали исключения в браузере -- только
    # в лог uvicorn/сервера. См. docs/ADMIN_GUIDE.md про логи веб-приложения.
    import logging

    logging.getLogger("webapp").exception("Необработанная ошибка на %s", request.url.path)
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})
    return templates.TemplateResponse("500.html", {"request": request}, status_code=500)


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def date_filters(date_from: Optional[str], date_to: Optional[str]):
    """Возвращает (datetime_from, datetime_to_exclusive, str_from, str_to) для запросов и форм.
    Без параметров — текущий календарный месяц."""
    if not date_from and not date_to:
        start, end = queries.month_bounds()
        return start, end, start.isoformat(), (end - timedelta(days=1)).isoformat()
    d_from = _parse_date(date_from)
    d_to_raw = _parse_date(date_to)
    d_to = (d_to_raw + timedelta(days=1)) if d_to_raw else None
    return d_from, d_to, date_from, date_to


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_login)):
    settings = get_settings()
    start, end = queries.month_bounds()
    t = queries.totals(db, date_from=start, date_to=end)
    top_depts = queries.by_department(db, date_from=start, date_to=end)[:5]
    top_users = queries.by_user(db, date_from=start, date_to=end)[:5]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": csrf_token(request),
            "site_code": settings.site_code,
            "totals": t,
            "top_depts": top_depts,
            "top_users": top_users,
            "month_label": start.strftime("%m.%Y"),
            "currency": settings.currency,
        },
    )


@app.get("/by-department")
def page_by_department(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_department(db, date_from=d_from, date_to=d_to)
    return templates.TemplateResponse(
        "by_department.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt, "currency": get_settings().currency,
        },
    )


@app.get("/by-user")
def page_by_user(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_user(db, date_from=d_from, date_to=d_to, department_id=department_id)
    return templates.TemplateResponse(
        "by_user.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt, "currency": get_settings().currency,
        },
    )


@app.get("/by-printer")
def page_by_printer(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_printer(db, date_from=d_from, date_to=d_to)
    return templates.TemplateResponse(
        "by_printer.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt, "currency": get_settings().currency,
        },
    )


@app.get("/export")
def export_page(request: Request, current_user: AppUser = Depends(require_login)):
    return templates.TemplateResponse(
        "export.html",
        {"request": request, "current_user": current_user, "csrf_token": csrf_token(request)},
    )


@app.get("/export/csv")
def export_csv(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    user_name: Optional[str] = None,
    printer_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    rows = queries.list_jobs(
        db, limit=1_000_000, date_from=d_from, date_to=d_to,
        department_id=department_id, user_name=user_name, printer_name=printer_name,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["time_created", "user_name", "department_id", "printer_name",
         "document_name", "total_pages", "is_color", "price_per_page", "cost"]
    )
    for j in rows:
        writer.writerow(
            [j.time_created.isoformat(), j.user_name, j.department_id, j.printer_name,
             j.document_name, j.total_pages, j.is_color, j.price_per_page, j.cost]
        )
    buf.seek(0)
    filename = f"print_jobs_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/print-jobs")
def api_print_jobs(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    user_name: Optional[str] = None,
    printer_name: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    rows = queries.list_jobs(
        db, limit=limit, offset=offset, date_from=d_from, date_to=d_to,
        department_id=department_id, user_name=user_name, printer_name=printer_name,
    )
    return [
        {
            "id": j.id,
            "time_created": j.time_created.isoformat(),
            "user_name": j.user_name,
            "department_id": j.department_id,
            "printer_name": j.printer_name,
            "document_name": j.document_name,
            "total_pages": j.total_pages,
            "is_color": j.is_color,
            "price_per_page": j.price_per_page,
            "cost": j.cost,
        }
        for j in rows
    ]


@app.get("/api/stats/by-department")
def api_by_department(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_login),
):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    return queries.by_department(db, date_from=d_from, date_to=d_to)


@app.get("/api/stats/by-user")
def api_by_user(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_login),
):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    return queries.by_user(db, date_from=d_from, date_to=d_to)


@app.get("/api/stats/by-printer")
def api_by_printer(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_login),
):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    return queries.by_printer(db, date_from=d_from, date_to=d_to)
