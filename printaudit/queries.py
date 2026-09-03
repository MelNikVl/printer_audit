from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from printaudit.models import Department, PrintJob

# color: "color" | "bw" | "unknown" | None (без фильтра) — см.
# printaudit.printers.resolver про tri-state is_color/color_source.
VALID_COLOR_FILTERS = ("color", "bw", "unknown")


def month_bounds(today: Optional[date] = None):
    today = today or date.today()
    start = today.replace(day=1)
    return start, today + timedelta(days=1)


def _apply_filters(
    query,
    date_from=None,
    date_to=None,
    department_id: Optional[int] = None,
    user_name: Optional[str] = None,
    printer_name: Optional[str] = None,
    site_id: Optional[int] = None,
    print_server_id: Optional[int] = None,
    color: Optional[str] = None,
    document_search: Optional[str] = None,
):
    if date_from:
        query = query.filter(PrintJob.time_created >= date_from)
    if date_to:
        query = query.filter(PrintJob.time_created < date_to)
    if department_id:
        query = query.filter(PrintJob.department_id == department_id)
    if user_name:
        query = query.filter(PrintJob.user_name == user_name)
    if printer_name:
        query = query.filter(PrintJob.printer_name == printer_name)
    if site_id:
        query = query.filter(PrintJob.site_id == site_id)
    if print_server_id:
        query = query.filter(PrintJob.print_server_id == print_server_id)
    if color == "color":
        query = query.filter(PrintJob.is_color.is_(True))
    elif color == "bw":
        query = query.filter(PrintJob.is_color.is_(False))
    elif color == "unknown":
        query = query.filter(PrintJob.is_color.is_(None))
    if document_search:
        query = query.filter(PrintJob.document_name.ilike(f"%{document_search}%"))
    return query


def totals(session: Session, **filters) -> dict:
    q = session.query(
        func.coalesce(func.sum(PrintJob.total_pages), 0),
        func.coalesce(func.sum(PrintJob.cost), 0.0),
        func.count(PrintJob.id),
    )
    q = _apply_filters(q, **filters)
    pages, cost, jobs = q.one()
    return {"pages": int(pages), "cost": float(cost), "jobs": int(jobs)}

def by_department(session: Session, **filters) -> list[dict]:
    q = (
        session.query(
            Department.id,
            Department.name,
            func.coalesce(func.sum(PrintJob.total_pages), 0),
            func.coalesce(func.sum(PrintJob.cost), 0.0),
            func.count(PrintJob.id),
        )
        .select_from(PrintJob)
        .outerjoin(Department, PrintJob.department_id == Department.id)
    )
    q = _apply_filters(q, **filters)
    q = q.group_by(Department.id, Department.name).order_by(func.sum(PrintJob.cost).desc())
    return [
        {
            "department_id": r[0],
            "department": r[1] or "(без отдела)",
            "pages": int(r[2]),
            "cost": float(r[3]),
            "jobs": int(r[4]),
        }
        for r in q.all()
    ]


def by_user(session: Session, **filters) -> list[dict]:
    q = session.query(
        PrintJob.user_name,
        func.coalesce(func.sum(PrintJob.total_pages), 0),
        func.coalesce(func.sum(PrintJob.cost), 0.0),
        func.count(PrintJob.id),
    )
    q = _apply_filters(q, **filters)
    q = q.group_by(PrintJob.user_name).order_by(func.sum(PrintJob.cost).desc())
    return [
        {"user_name": r[0], "pages": int(r[1]), "cost": float(r[2]), "jobs": int(r[3])}
        for r in q.all()
    ]


def by_printer(session: Session, **filters) -> list[dict]:
    q = session.query(
        PrintJob.printer_name,
        func.coalesce(func.sum(PrintJob.total_pages), 0),
        func.coalesce(func.sum(PrintJob.cost), 0.0),
        func.count(PrintJob.id),
    )
    q = _apply_filters(q, **filters)
    q = q.group_by(PrintJob.printer_name).order_by(func.sum(PrintJob.cost).desc())
    return [
        {"printer_name": r[0], "pages": int(r[1]), "cost": float(r[2]), "jobs": int(r[3])}
        for r in q.all()
    ]


def list_jobs(session: Session, limit: int = 200, offset: int = 0, **filters) -> list[PrintJob]:
    q = session.query(PrintJob).order_by(PrintJob.time_created.desc(), PrintJob.id.desc())
    q = _apply_filters(q, **filters)
    return q.offset(offset).limit(limit).all()


def count_jobs(session: Session, **filters) -> int:
    q = session.query(func.count(PrintJob.id))
    q = _apply_filters(q, **filters)
    return int(q.scalar() or 0)
