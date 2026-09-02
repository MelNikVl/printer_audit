from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from printaudit.models import Department, PrintJob


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
    q = session.query(PrintJob).order_by(PrintJob.time_created.desc())
    q = _apply_filters(q, **filters)
    return q.offset(offset).limit(limit).all()
