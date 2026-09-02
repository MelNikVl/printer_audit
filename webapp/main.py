"""Веб-UI и JSON API для отчётов по печати. Запуск (из корня репозитория):

    python -m uvicorn webapp.main:app --host 0.0.0.0 --port 8000

См. deploy/run_webapp.ps1 для готового скрипта запуска и docs/ADMIN_GUIDE.md
для варианта установки как службы Windows (NSSM).
"""
import csv
import io
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from printaudit import queries  # noqa: E402
from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Print Audit")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
def dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    start, end = queries.month_bounds()
    t = queries.totals(db, date_from=start, date_to=end)
    top_depts = queries.by_department(db, date_from=start, date_to=end)[:5]
    top_users = queries.by_user(db, date_from=start, date_to=end)[:5]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
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
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_department(db, date_from=d_from, date_to=d_to)
    return templates.TemplateResponse(
        "by_department.html",
        {"request": request, "rows": rows, "date_from": df, "date_to": dt,
         "currency": get_settings().currency},
    )


@app.get("/by-user")
def page_by_user(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_user(db, date_from=d_from, date_to=d_to, department_id=department_id)
    return templates.TemplateResponse(
        "by_user.html",
        {"request": request, "rows": rows, "date_from": df, "date_to": dt,
         "currency": get_settings().currency},
    )


@app.get("/by-printer")
def page_by_printer(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    d_from, d_to, df, dt = date_filters(date_from, date_to)
    rows = queries.by_printer(db, date_from=d_from, date_to=d_to)
    return templates.TemplateResponse(
        "by_printer.html",
        {"request": request, "rows": rows, "date_from": df, "date_to": dt,
         "currency": get_settings().currency},
    )


@app.get("/export")
def export_page(request: Request):
    return templates.TemplateResponse("export.html", {"request": request})


@app.get("/export/csv")
def export_csv(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department_id: Optional[int] = None,
    user_name: Optional[str] = None,
    printer_name: Optional[str] = None,
    db: Session = Depends(get_db),
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
def api_by_department(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    return queries.by_department(db, date_from=d_from, date_to=d_to)


@app.get("/api/stats/by-user")
def api_by_user(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    return queries.by_user(db, date_from=d_from, date_to=d_to)


@app.get("/api/stats/by-printer")
def api_by_printer(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to, _, _ = date_filters(date_from, date_to)
    return queries.by_printer(db, date_from=d_from, date_to=d_to)
