"""Площадки (Site) и регистрации Print Server/агентов (PrintServer).

Локальный коллектор (standalone- и agent-режим) сам заводит себе "неявную"
пару Site+PrintServer при первом запуске (см. get_or_create_site/
get_or_create_print_server ниже) — без этого print_jobs.print_server_id
никогда не заполнялся бы для локально собранных заданий, и новая
идемпотентность (print_server_id, record_id) не имела бы смысла (см.
printaudit.models.PrintJob). Никакого токена агента для этого не нужно —
это просто строки в ЛОКАЛЬНОЙ БД того же процесса, а не обращение к
центральному API (для центрального API см. printaudit.security.agent_tokens
и webapp/agent_api.py — там print_server ДОЛЖЕН существовать заранее,
создание через API запрещено).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from printaudit.models import PrintServer, Site

ONLINE_THRESHOLD_MINUTES = 5
WARNING_THRESHOLD_MINUTES = 30

STATUS_DISABLED = "disabled"
STATUS_PENDING = "pending"
STATUS_ONLINE = "online"
STATUS_WARNING = "warning"
STATUS_OFFLINE = "offline"


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def compute_status(server: PrintServer, now: Optional[datetime] = None) -> str:
    """Статус НЕ хранится как поле — вычисляется по возрасту last_heartbeat_at,
    иначе агент, который перестал отвечать, навсегда остался бы "online" в БД."""
    if server.is_disabled:
        return STATUS_DISABLED
    if server.last_heartbeat_at is None:
        return STATUS_PENDING
    now = _naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    last = _naive_utc(server.last_heartbeat_at)
    age = now - last
    if age <= timedelta(minutes=ONLINE_THRESHOLD_MINUTES):
        return STATUS_ONLINE
    if age <= timedelta(minutes=WARNING_THRESHOLD_MINUTES):
        return STATUS_WARNING
    return STATUS_OFFLINE


def get_or_create_site(session: Session, site_code: str, name: Optional[str] = None) -> Site:
    site_code = (site_code or "").strip()
    site = session.query(Site).filter_by(site_code=site_code).first()
    if site is None:
        site = Site(site_code=site_code, name=name or site_code, is_active=True)
        session.add(site)
        session.flush()
    return site


def get_or_create_print_server(
    session: Session,
    site: Site,
    server_name: str,
    *,
    agent_version: Optional[str] = None,
    protocol_version: Optional[int] = None,
) -> PrintServer:
    server_name = (server_name or "").strip() or "UNKNOWN"
    server = (
        session.query(PrintServer)
        .filter_by(site_id=site.id, server_name=server_name)
        .first()
    )
    if server is None:
        server = PrintServer(
            site_id=site.id,
            server_name=server_name,
            display_name=server_name,
            agent_version=agent_version,
            protocol_version=protocol_version,
        )
        session.add(server)
        session.flush()
    return server


def get_or_create_local_print_server(session: Session, settings=None) -> PrintServer:
    """Локальный (standalone/agent) Print Server ЭТОГО процесса — заводится
    автоматически из config.yaml (site_code, server_name), без единого
    администраторского действия. Используется коллектором и обнаружением
    принтеров (Get-Printer), чтобы print_jobs/printer_queues всегда имели
    print_server_id даже без центральной регистрации (см. printaudit.models.PrintJob).

    Не путать с print_server, который агент шлёт в центр по токену — тот
    ДОЛЖЕН существовать заранее (создаётся администратором в
    /admin/print-servers), см. printaudit.security.agent_tokens/webapp/agent_api.py."""
    if settings is None:
        from printaudit.config import get_settings

        settings = get_settings()
    site = get_or_create_site(session, settings.site_code)
    return get_or_create_print_server(session, site, settings.server_name)
