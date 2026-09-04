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
from printaudit.agent_settings import get_agent_settings  # noqa: E402
from printaudit.config import get_settings  # noqa: E402
from printaudit.models import AppUser, Department, PrintJob, PrintServer, Site  # noqa: E402
from webapp import admin_routes, agent_api, auth_routes, endpoint_api, printers_routes  # noqa: E402
from webapp.deps import csrf_token, get_db, require_login  # noqa: E402
from webapp.errors import Forbidden, MustChangePassword, NotAuthenticated  # noqa: E402
from webapp.middleware import CsrfCookieMiddleware, TrustedProxyHeadersMiddleware  # noqa: E402
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
    # Тот же принцип для APP_MODE: get_agent_settings() бросает
    # InvalidAppModeError, если APP_MODE задан, но не является ни
    # standalone/agent/central — опечатка в .env не должна незаметно
    # откатывать сервер к поведению по умолчанию (см. printaudit/agent_settings.py).
    get_agent_settings()
    yield


app = FastAPI(title="Print Audit", lifespan=lifespan)
app.add_middleware(CsrfCookieMiddleware)
app.add_middleware(agent_api.MaxBodySizeMiddleware)
app.add_middleware(endpoint_api.MaxEndpointBodySizeMiddleware)
app.add_middleware(TrustedProxyHeadersMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(agent_api.router)
app.include_router(endpoint_api.router)
app.include_router(printers_routes.router)


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/") or path.startswith("/export/csv")


@app.exception_handler(NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: NotAuthenticated):
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=401, content={"detail": "Требуется вход в систему"})
    return RedirectResponse(url=f"/login?next={exc.next_path}", status_code=303)


@app.exception_handler(MustChangePassword)
async def _must_change_password_handler(request: Request, exc: MustChangePassword):
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=403, content={"detail": "Требуется смена пароля"})
    return RedirectResponse(url="/change-password", status_code=303)


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


def _safe_validation_errors(exc: RequestValidationError) -> list:
    """pydantic's exc.errors() включает "input" (сырое значение поля ровно
    таким, каким его прислал клиент) и "ctx" (может содержать объект
    исключения из кастомного @field_validator, например ValueError -- см.
    webapp/agent_api.py::_reject_non_finite) — ни то, ни другое не
    гарантированно сериализуется в JSON (json.dumps с allow_nan=False,
    как у Starlette JSONResponse, падает на float NaN/Infinity; объект
    исключения не сериализуется вообще никогда). Раньше это приводило к
    500 при попытке ответить 422 на как раз тот невалидный ввод, который
    и должен был получить понятную ошибку. Отдаём только заведомо
    JSON-safe поля (type/loc/msg) — это заодно не отражает обратно
    клиенту произвольные присланные им значения."""
    return [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    if _is_api_path(request.url.path):
        return JSONResponse(status_code=422, content={"detail": _safe_validation_errors(exc)})
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
def dashboard(
    request: Request,
    site_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    settings = get_settings()
    start, end = queries.month_bounds()
    t = queries.totals(db, date_from=start, date_to=end, site_id=site_id)
    top_depts = queries.by_department(db, date_from=start, date_to=end, site_id=site_id)[:5]
    top_users = queries.by_user(db, date_from=start, date_to=end, site_id=site_id)[:5]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": csrf_token(request),
            "site_code": settings.site_code,
            "site_id": site_id,
            "sites": db.query(Site).order_by(Site.name).all(),
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
    site_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_department(db, date_from=d_from, date_to=d_to, site_id=site_id)
    return templates.TemplateResponse(
        "by_department.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt, "currency": get_settings().currency,
            "site_id": site_id, "sites": db.query(Site).order_by(Site.name).all(),
        },
    )


@app.get("/by-user")
def page_by_user(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    site_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_user(db, date_from=d_from, date_to=d_to, department_id=department_id, site_id=site_id)
    return templates.TemplateResponse(
        "by_user.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt, "currency": get_settings().currency,
            "site_id": site_id, "sites": db.query(Site).order_by(Site.name).all(),
        },
    )


@app.get("/by-printer")
def page_by_printer(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    site_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_printer(db, date_from=d_from, date_to=d_to, site_id=site_id)
    return templates.TemplateResponse(
        "by_printer.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt, "currency": get_settings().currency,
            "site_id": site_id, "sites": db.query(Site).order_by(Site.name).all(),
        },
    )


MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def _pagination(page: Optional[int], page_size: Optional[int]):
    page = page if page and page > 0 else 1
    page_size = page_size if page_size and page_size > 0 else DEFAULT_PAGE_SIZE
    return page, min(page_size, MAX_PAGE_SIZE)


def _job_filters(
    date_from, date_to, department_id, user_name, printer_name,
    site_id, print_server_id, color, q,
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    filters = dict(
        date_from=d_from, date_to=d_to, department_id=department_id, user_name=user_name,
        printer_name=printer_name, site_id=site_id, print_server_id=print_server_id,
        color=color or None, document_search=q or None,
    )
    return filters, df, dt


def _job_to_dict(j: PrintJob) -> dict:
    """Единое представление задания для журнала /print-jobs, /api/print-jobs
    и CSV-экспорта — те же поля везде (см. требование "расширить API и CSV
    этими же полями")."""
    return {
        "id": j.id,
        "job_id": j.job_id,
        "time_created": j.time_created.isoformat(),
        "site_id": j.site_id,
        "site": j.site.name if j.site else j.site_code,
        "print_server_id": j.print_server_id,
        "print_server": (j.print_server.display_name or j.print_server.server_name) if j.print_server else None,
        "user_name": j.user_name,
        "department_id": j.department_id,
        "department": j.department.name if j.department else None,
        "printer_name": j.printer_name,
        "document_name": j.document_name,
        "source_computer": j.source_computer,
        "total_pages": j.total_pages,
        "copies": j.copies,
        "pages_per_copy": j.pages_per_copy,
        "is_color": j.is_color,
        "color_source": j.color_source,
        "price_per_page": j.price_per_page,
        "currency": j.currency,
        "cost": j.cost,
    }


@app.get("/print-jobs")
def page_print_jobs(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    user_name: Optional[str] = None,
    printer_name: Optional[str] = None,
    site_id: Optional[int] = None,
    print_server_id: Optional[int] = None,
    color: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    """Построчный журнал заданий печати (в отличие от агрегированных
    /by-user, /by-printer) — с серверными фильтрами и серверной пагинацией
    (вся таблица никогда не грузится в браузер целиком)."""
    filters, df, dt = _job_filters(
        date_from, date_to, department_id, user_name, printer_name, site_id, print_server_id, color, q
    )
    page, page_size = _pagination(page, page_size)
    total = queries.count_jobs(db, **filters)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = queries.list_jobs(db, limit=page_size, offset=(page - 1) * page_size, **filters)

    return templates.TemplateResponse(
        "print_jobs.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "date_from": df, "date_to": dt,
            "department_id": department_id, "user_name": user_name or "", "printer_name": printer_name or "",
            "site_id": site_id, "print_server_id": print_server_id, "color": color or "", "q": q or "",
            "sites": db.query(Site).order_by(Site.name).all(),
            "print_servers": db.query(PrintServer).order_by(PrintServer.server_name).all(),
            "departments": db.query(Department).filter_by(is_active=True).order_by(Department.name).all(),
            "page": page, "page_size": page_size, "total": total, "total_pages": total_pages,
            "currency": get_settings().currency,
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
    site_id: Optional[int] = None,
    print_server_id: Optional[int] = None,
    color: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    filters, _, _ = _job_filters(
        date_from, date_to, department_id, user_name, printer_name, site_id, print_server_id, color, q
    )
    rows = queries.list_jobs(db, limit=1_000_000, **filters)
    buf = io.StringIO()
    writer = csv.writer(buf)
    columns = [
        "id", "job_id", "time_created", "site", "print_server", "user_name", "department",
        "printer_name", "document_name", "source_computer", "total_pages", "copies",
        "pages_per_copy", "is_color", "color_source", "price_per_page", "currency", "cost",
    ]
    writer.writerow(columns)
    for j in rows:
        row = _job_to_dict(j)
        writer.writerow([row[c] for c in columns])
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
    site_id: Optional[int] = None,
    print_server_id: Optional[int] = None,
    color: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    filters, _, _ = _job_filters(
        date_from, date_to, department_id, user_name, printer_name, site_id, print_server_id, color, q
    )
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)
    total = queries.count_jobs(db, **filters)
    rows = queries.list_jobs(db, limit=limit, offset=offset, **filters)
    return JSONResponse(
        content=[_job_to_dict(j) for j in rows],
        headers={"X-Total-Count": str(total)},
    )


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
