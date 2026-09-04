"""Раздел /admin — обзор, администраторы, отделы, AD-пользователи/группы и
правила, принтеры, тарифы. Каждый роут явно требует роль через
require_role(...) на уровне зависимости — это работает независимо от того,
показан ли пункт меню (webapp/templates/base.html прячет "Администрирование"
для viewer, но сам роут защищён отдельно и не полагается на скрытие ссылки)."""
from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from printaudit import roles
from printaudit.ad.client import ADClient, ADError
from printaudit.ad_normalize import normalize_login
from printaudit.ad_settings import AuthAvailability, get_ad_settings
from printaudit.agent_settings import get_agent_settings
from printaudit.admin_users import (
    AdminActionError,
    create_local_user,
    delete_admin_assignment,
    disable_admin,
    upsert_admin_assignment,
)
from printaudit.audit import record as audit_record
from printaudit.config import get_settings
from printaudit.department_resolver import apply_ad_department_sync, plan_ad_department_sync
from printaudit.models import (
    AdDepartmentRule,
    AdGroup,
    AdGroupMembership,
    AdUser,
    AppUser,
    CollectorState,
    Department,
    EndpointAgent,
    PriceRule,
    PrinterDevice,
    PrinterDeviceQueueLink,
    PrinterQueue,
    PrintJob,
    PrintServer,
    Site,
    SnmpProfile,
    SyncRun,
)
from printaudit.models import User as LegacyUser
from printaudit.monitoring.device_queries import dashboard_summary
from printaudit.monitoring.devices import DeviceActionError, create_device, link_queue, set_monitoring_source, unlink_queue
from printaudit.monitoring.snmp_profiles import SnmpProfileError, create_snmp_profile, set_snmp_profile_active, update_snmp_profile
from printaudit.printers.discovery import PrinterDiscoveryError, sync_printer_queues
from printaudit.printers.resolver import resolve_price
from printaudit.security.agent_tokens import generate_agent_token, hash_agent_token
from printaudit.sites import compute_status, get_or_create_local_print_server
from printaudit.timeutil import naive_utc, utcnow
from webapp.deps import (
    csrf_token,
    get_ad_client,
    get_auth_availability_dep,
    get_client_ip,
    get_db,
    require_csrf,
    require_role,
)
from webapp.errors import safe_error_message
from webapp.templating import templates

router = APIRouter(prefix="/admin")

ADMIN_ROLES = (roles.ADMIN, roles.SUPERADMIN)


def _redirect(path: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{path}{query}", status_code=303)


def _parse_date_or_none(value: str) -> Optional[datetime]:
    return datetime.strptime(value, "%Y-%m-%d") if value else None


# ---------------------------------------------------------------------------
# Обзор
# ---------------------------------------------------------------------------


@router.get("")
def admin_overview(
    request: Request,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    ad_settings = get_ad_settings()
    last_ad_sync = db.query(SyncRun).filter_by(run_type="ad_sync").order_by(SyncRun.started_at.desc()).first()
    last_printer_sync = (
        db.query(SyncRun).filter_by(run_type="printer_discovery").order_by(SyncRun.started_at.desc()).first()
    )
    last_collector_run = (
        db.query(SyncRun).filter_by(run_type="collector").order_by(SyncRun.started_at.desc()).first()
    )
    recent_failed_runs = (
        db.query(SyncRun).filter_by(status="failed").order_by(SyncRun.started_at.desc()).limit(5).all()
    )
    collector_states = db.query(CollectorState).all()
    monitoring_summary = dashboard_summary(db)

    return templates.TemplateResponse(
        "admin/overview.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": csrf_token(request),
            "ad_configured": ad_settings.is_configured,
            "ad_server": ad_settings.server,
            "ad_bind_configured": bool(ad_settings.bind_user),
            "last_ad_sync": last_ad_sync,
            "ad_user_count": db.query(AdUser).count(),
            "ad_group_count": db.query(AdGroup).count(),
            "department_count": db.query(Department).filter_by(is_active=True).count(),
            "printer_queue_count": db.query(PrinterQueue).filter_by(is_active=True).count(),
            "printer_queue_missing_count": db.query(PrinterQueue).filter_by(is_active=False).count(),
            "last_printer_sync": last_printer_sync,
            "last_collector_run": last_collector_run,
            "collector_states": collector_states,
            "recent_failed_runs": recent_failed_runs,
            "monitoring_summary": monitoring_summary,
        },
    )


# ---------------------------------------------------------------------------
# Администраторы (только superadmin)
# ---------------------------------------------------------------------------


@router.get("/administrators")
def admin_administrators(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(roles.SUPERADMIN)),
    ad_client: ADClient = Depends(get_ad_client),
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    admins = db.query(AppUser).order_by(AppUser.role.desc(), AppUser.login_normalized).all()
    ad_results, ad_search_error = [], None
    # Ни одного обращения к LDAP, если AD выключен -- ad_client.search_users()
    # не вызывается вообще, не только "вызывается и ошибка скрывается".
    if auth_availability.ad_enabled and q.strip():
        try:
            ad_results = ad_client.search_users(q.strip())
        except ADError as exc:
            ad_search_error = safe_error_message(exc, "поиск пользователей в AD")
    return templates.TemplateResponse(
        "admin/administrators.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "admins": admins, "q": q, "ad_results": ad_results, "ad_search_error": ad_search_error,
            "all_roles": roles.ALL_ROLES, "auth": auth_availability,
        },
    )


@router.post("/administrators/create-local", dependencies=[Depends(require_csrf)])
def admin_administrators_create_local(
    request: Request,
    login: str = Form(...),
    role: Literal["viewer", "admin", "superadmin"] = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(roles.SUPERADMIN)),
):
    try:
        user, temp_password = create_local_user(
            db, actor=current_user, login=login, role=role,
            display_name=display_name or None, email=email or None,
            ip_address=get_client_ip(request),
        )
    except AdminActionError as exc:
        db.rollback()
        return _redirect("/admin/administrators", err=str(exc))
    db.commit()
    return _redirect(
        "/admin/administrators",
        msg=(
            f"Локальный пользователь {user.login_normalized} создан. Временный пароль "
            f"(показывается один раз, скопируйте и передайте пользователю): {temp_password}"
        ),
    )


@router.post("/administrators/assign", dependencies=[Depends(require_csrf)])
def admin_administrators_assign(
    request: Request,
    login: str = Form(...),
    role: Literal["viewer", "admin", "superadmin"] = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    ad_sid: str = Form(""),
    ad_object_guid: str = Form(""),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(roles.SUPERADMIN)),
):
    login_normalized = normalize_login(login)
    try:
        upsert_admin_assignment(
            db, actor=current_user, login_normalized=login_normalized, role=role,
            ad_sid=ad_sid or None, ad_object_guid=ad_object_guid or None,
            display_name=display_name or None, email=email or None,
            ip_address=get_client_ip(request),
        )
    except AdminActionError as exc:
        db.rollback()
        return _redirect("/admin/administrators", err=str(exc))
    db.commit()
    return _redirect("/admin/administrators", msg=f"Назначение для {login_normalized} сохранено")


@router.post("/administrators/{app_user_id}/disable", dependencies=[Depends(require_csrf)])
def admin_administrators_disable(
    app_user_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(roles.SUPERADMIN)),
):
    target = db.get(AppUser, app_user_id)
    if target is None:
        return _redirect("/admin/administrators", err="Пользователь не найден")
    try:
        disable_admin(db, actor=current_user, target=target, ip_address=get_client_ip(request))
    except AdminActionError as exc:
        db.rollback()
        return _redirect("/admin/administrators", err=str(exc))
    db.commit()
    return _redirect("/admin/administrators", msg=f"Доступ для {target.login_normalized} отключён")


@router.post("/administrators/{app_user_id}/delete", dependencies=[Depends(require_csrf)])
def admin_administrators_delete(
    app_user_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(roles.SUPERADMIN)),
):
    target = db.get(AppUser, app_user_id)
    if target is None:
        return _redirect("/admin/administrators", err="Пользователь не найден")
    try:
        delete_admin_assignment(db, actor=current_user, target=target, ip_address=get_client_ip(request))
    except AdminActionError as exc:
        db.rollback()
        return _redirect("/admin/administrators", err=str(exc))
    db.commit()
    return _redirect("/admin/administrators", msg="Назначение удалено")


# ---------------------------------------------------------------------------
# Отделы
# ---------------------------------------------------------------------------


@router.get("/departments")
def admin_departments(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    departments = db.query(Department).order_by(Department.display_order, Department.name).all()
    return templates.TemplateResponse(
        "admin/departments.html",
        {"request": request, "current_user": current_user, "csrf_token": csrf_token(request), "departments": departments},
    )


@router.post("/departments/create", dependencies=[Depends(require_csrf)])
def admin_departments_create(
    request: Request,
    name: str = Form(...), cost_center_code: str = Form(""), description: str = Form(""), display_order: int = Form(0),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    name = name.strip()
    if not name:
        return _redirect("/admin/departments", err="Название отдела обязательно")
    if db.query(Department).filter_by(name=name).first():
        return _redirect("/admin/departments", err=f"Отдел «{name}» уже существует")
    dept = Department(
        name=name, cost_center_code=cost_center_code or None, description=description or None,
        display_order=display_order, is_active=True,
    )
    db.add(dept)
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="department.create", object_type="department",
        object_id=dept.id, new_value={"name": name}, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/departments", msg=f"Отдел «{name}» создан")


@router.post("/departments/{department_id}/update", dependencies=[Depends(require_csrf)])
def admin_departments_update(
    department_id: int, request: Request,
    name: str = Form(...), cost_center_code: str = Form(""), description: str = Form(""),
    display_order: int = Form(0), is_active: Optional[str] = Form(None),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    # HTML-чекбокс НЕ отправляется вовсе, если он снят -- поэтому здесь строка
    # ("true" | None), а не bool с дефолтом True (иначе снятие галочки в форме
    # никогда не привело бы к archив/is_active=False).
    dept = db.get(Department, department_id)
    if dept is None:
        return _redirect("/admin/departments", err="Отдел не найден")
    old = {"name": dept.name, "is_active": dept.is_active}
    dept.name = name.strip() or dept.name
    dept.cost_center_code = cost_center_code or None
    dept.description = description or None
    dept.display_order = display_order
    dept.is_active = is_active == "true"
    audit_record(
        db, actor_app_user_id=current_user.id, action="department.update", object_type="department",
        object_id=dept.id, old_value=old, new_value={"name": dept.name, "is_active": dept.is_active},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/departments", msg="Отдел обновлён")


@router.post("/departments/{department_id}/delete", dependencies=[Depends(require_csrf)])
def admin_departments_delete(
    department_id: int, request: Request,
    reassign_to: Optional[int] = Form(None),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    dept = db.get(Department, department_id)
    if dept is None:
        return _redirect("/admin/departments", err="Отдел не найден")

    refs = (
        db.query(LegacyUser).filter_by(department_id=department_id).count()
        + db.query(AdUser).filter_by(department_id=department_id).count()
        + db.query(PrintJob).filter_by(department_id=department_id).count()
    )
    if refs and not reassign_to:
        return _redirect(
            "/admin/departments",
            err=(
                f"У отдела «{dept.name}» есть привязанные пользователи/задания ({refs}). "
                f"Укажите отдел для переназначения при удалении, либо снимите галку «Активен» "
                f"вместо удаления (архивирование)."
            ),
        )
    if refs and reassign_to:
        target = db.get(Department, reassign_to)
        if target is None:
            return _redirect("/admin/departments", err="Отдел для переназначения не найден")
        db.query(LegacyUser).filter_by(department_id=department_id).update({"department_id": reassign_to})
        db.query(AdUser).filter_by(department_id=department_id).update({"department_id": reassign_to})
        db.query(PrintJob).filter_by(department_id=department_id).update({"department_id": reassign_to})

    audit_record(
        db, actor_app_user_id=current_user.id, action="department.delete", object_type="department",
        object_id=dept.id, old_value={"name": dept.name}, ip_address=get_client_ip(request),
    )
    db.delete(dept)
    db.commit()
    suffix = " (пользователи и задания переназначены)" if refs else ""
    return _redirect("/admin/departments", msg=f"Отдел удалён{suffix}")


# ---------------------------------------------------------------------------
# Пользователи AD
# ---------------------------------------------------------------------------


@router.get("/ad-users")
def admin_ad_users(
    request: Request, q: str = "",
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
    ad_client: ADClient = Depends(get_ad_client),
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    imported = db.query(AdUser).order_by(AdUser.login_normalized).all()
    departments = db.query(Department).filter_by(is_active=True).order_by(Department.name).all()
    ad_results, ad_search_error = [], None
    if auth_availability.ad_enabled and q.strip():
        try:
            ad_results = ad_client.search_users(q.strip())
        except ADError as exc:
            ad_search_error = safe_error_message(exc, "поиск пользователей в AD")
    imported_logins = {u.login_normalized for u in imported}
    return templates.TemplateResponse(
        "admin/ad_users.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "imported": imported, "departments": departments, "q": q,
            "ad_results": ad_results, "ad_search_error": ad_search_error, "imported_logins": imported_logins,
            "auth": auth_availability,
        },
    )


@router.post("/ad-users/import", dependencies=[Depends(require_csrf)])
def admin_ad_users_import(
    request: Request,
    sam_account_name: str = Form(...), login: str = Form(...), domain: str = Form(""),
    display_name: str = Form(""), email: str = Form(""), sid: str = Form(""), object_guid: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    login_normalized = normalize_login(login)
    existing = db.query(AdUser).filter_by(login_normalized=login_normalized).first()
    if existing is None:
        existing = AdUser(login_normalized=login_normalized, sam_account_name=sam_account_name)
        db.add(existing)
    existing.sid = sid or existing.sid
    existing.object_guid = object_guid or existing.object_guid
    existing.domain = domain or existing.domain
    existing.display_name = display_name or existing.display_name
    existing.email = email or existing.email
    existing.last_synced_at = utcnow()
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="ad_user.import", object_type="ad_user",
        object_id=existing.id, new_value={"login": login_normalized}, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/ad-users", msg=f"Пользователь {login_normalized} импортирован")


@router.post("/ad-users/{ad_user_id}/department", dependencies=[Depends(require_csrf)])
def admin_ad_users_department(
    ad_user_id: int, request: Request,
    department_id: Optional[int] = Form(None), locked: bool = Form(False),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    ad_user = db.get(AdUser, ad_user_id)
    if ad_user is None:
        return _redirect("/admin/ad-users", err="Пользователь не найден")
    old = {"department_id": ad_user.department_id, "department_locked": ad_user.department_locked}
    ad_user.department_id = department_id
    ad_user.department_source = "manual"
    ad_user.department_locked = locked
    ad_user.department_rule_id = None
    ad_user.updated_at = utcnow()
    audit_record(
        db, actor_app_user_id=current_user.id, action="ad_user.set_department", object_type="ad_user",
        object_id=ad_user.id, old_value=old, new_value={"department_id": department_id, "locked": locked},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/ad-users", msg="Отдел сохранён")


@router.post("/ad-users/{ad_user_id}/disable", dependencies=[Depends(require_csrf)])
def admin_ad_users_disable(
    ad_user_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    ad_user = db.get(AdUser, ad_user_id)
    if ad_user is None:
        return _redirect("/admin/ad-users", err="Пользователь не найден")
    ad_user.local_disabled = True
    audit_record(
        db, actor_app_user_id=current_user.id, action="ad_user.disable", object_type="ad_user",
        object_id=ad_user.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/ad-users", msg="Пользователь отключён локально")


@router.post("/ad-users/{ad_user_id}/enable", dependencies=[Depends(require_csrf)])
def admin_ad_users_enable(
    ad_user_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    ad_user = db.get(AdUser, ad_user_id)
    if ad_user is None:
        return _redirect("/admin/ad-users", err="Пользователь не найден")
    ad_user.local_disabled = False
    audit_record(
        db, actor_app_user_id=current_user.id, action="ad_user.enable", object_type="ad_user",
        object_id=ad_user.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/ad-users", msg="Пользователь включён")


@router.post("/ad-users/resync", dependencies=[Depends(require_csrf)])
def admin_ad_users_resync(
    request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
    ad_client: ADClient = Depends(get_ad_client),
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    if not auth_availability.ad_enabled:
        return _redirect("/admin/ad-users", err="AD отключён — синхронизация недоступна.")
    started = utcnow()
    updated, errors = 0, 0
    for ad_user in db.query(AdUser).all():
        try:
            principal = ad_client.get_user_by_login(ad_user.login_normalized)
        except ADError:
            errors += 1
            continue
        if principal is None:
            continue
        ad_user.sid = principal.sid or ad_user.sid
        ad_user.object_guid = principal.object_guid or ad_user.object_guid
        ad_user.display_name = principal.display_name or ad_user.display_name
        ad_user.email = principal.email or ad_user.email
        ad_user.last_synced_at = utcnow()
        updated += 1
    db.add(
        SyncRun(
            run_type="ad_sync", started_at=started, finished_at=utcnow(),
            status="success", inserted=updated, skipped=errors,
        )
    )
    db.commit()
    return _redirect("/admin/ad-users", msg=f"Синхронизация завершена: обновлено {updated}, ошибок {errors}")


# ---------------------------------------------------------------------------
# Группы AD и правила распределения по отделам
# ---------------------------------------------------------------------------


@router.get("/ad-groups")
def admin_ad_groups(
    request: Request, q: str = "",
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
    ad_client: ADClient = Depends(get_ad_client),
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    groups = db.query(AdGroup).order_by(AdGroup.display_name).all()
    rules_by_group = {r.ad_group_id: r for r in db.query(AdDepartmentRule).all()}
    member_counts = dict(
        db.query(AdGroupMembership.ad_group_id, func.count(AdGroupMembership.id))
        .group_by(AdGroupMembership.ad_group_id)
        .all()
    )
    departments = db.query(Department).filter_by(is_active=True).order_by(Department.name).all()
    ad_results, ad_search_error = [], None
    if auth_availability.ad_enabled and q.strip():
        try:
            ad_results = ad_client.search_groups(q.strip())
        except ADError as exc:
            ad_search_error = safe_error_message(exc, "поиск групп в AD")
    imported_dns = {g.dn for g in groups}
    return templates.TemplateResponse(
        "admin/ad_groups.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "groups": groups, "rules_by_group": rules_by_group, "member_counts": member_counts,
            "departments": departments, "q": q, "ad_results": ad_results, "ad_search_error": ad_search_error,
            "imported_dns": imported_dns, "auth": auth_availability,
        },
    )


@router.post("/ad-groups/import", dependencies=[Depends(require_csrf)])
def admin_ad_groups_import(
    request: Request,
    dn: str = Form(...), sam_account_name: str = Form(""), display_name: str = Form(""), description: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    existing = db.query(AdGroup).filter_by(dn=dn).first()
    if existing is None:
        existing = AdGroup(
            dn=dn, sam_account_name=sam_account_name, display_name=display_name or sam_account_name,
            description=description or None,
        )
        db.add(existing)
        db.flush()
        audit_record(
            db, actor_app_user_id=current_user.id, action="ad_group.import", object_type="ad_group",
            object_id=existing.id, new_value={"dn": dn}, ip_address=get_client_ip(request),
        )
    existing.last_synced_at = utcnow()
    db.commit()
    return _redirect("/admin/ad-groups", msg=f"Группа {display_name or sam_account_name} импортирована")


@router.post("/ad-groups/{group_id}/sync-members", dependencies=[Depends(require_csrf)])
def admin_ad_groups_sync_members(
    group_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
    ad_client: ADClient = Depends(get_ad_client),
    auth_availability: AuthAvailability = Depends(get_auth_availability_dep),
):
    if not auth_availability.ad_enabled:
        return _redirect("/admin/ad-groups", err="AD отключён — синхронизация недоступна.")
    group = db.get(AdGroup, group_id)
    if group is None:
        return _redirect("/admin/ad-groups", err="Группа не найдена")
    try:
        members = ad_client.get_group_members(group.dn)
    except ADError as exc:
        return _redirect("/admin/ad-groups", err=safe_error_message(exc, "синхронизация участников группы AD"))

    current_ids = set()
    for principal in members:
        ad_user = db.query(AdUser).filter_by(login_normalized=principal.login_normalized).first()
        if ad_user is None:
            ad_user = AdUser(
                sam_account_name=principal.sam_account_name, login_normalized=principal.login_normalized,
                domain=principal.domain, display_name=principal.display_name, email=principal.email,
                sid=principal.sid, object_guid=principal.object_guid, last_synced_at=utcnow(),
            )
            db.add(ad_user)
            db.flush()
        membership = (
            db.query(AdGroupMembership).filter_by(ad_group_id=group.id, ad_user_id=ad_user.id).first()
        )
        if membership is None:
            db.add(AdGroupMembership(ad_group_id=group.id, ad_user_id=ad_user.id))
        current_ids.add(ad_user.id)

    stale = (
        db.query(AdGroupMembership)
        .filter_by(ad_group_id=group.id)
        .filter(~AdGroupMembership.ad_user_id.in_(current_ids))
        .all()
    )
    for membership in stale:
        db.delete(membership)

    group.last_synced_at = utcnow()
    db.add(SyncRun(run_type="ad_sync", started_at=utcnow(), finished_at=utcnow(), status="success", inserted=len(members)))
    db.commit()
    return _redirect("/admin/ad-groups", msg=f"Синхронизировано участников группы: {len(members)}")


@router.post("/ad-groups/{group_id}/rule", dependencies=[Depends(require_csrf)])
def admin_ad_groups_set_rule(
    group_id: int, request: Request,
    department_id: int = Form(...), priority: int = Form(0), is_active: Optional[str] = Form(None),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    # См. комментарий в /admin/departments/{id}/update про чекбоксы и Form(bool).
    is_active_bool = is_active == "true"
    group = db.get(AdGroup, group_id)
    if group is None:
        return _redirect("/admin/ad-groups", err="Группа не найдена")
    rule = db.query(AdDepartmentRule).filter_by(ad_group_id=group_id).first()
    old = None
    if rule is None:
        rule = AdDepartmentRule(
            ad_group_id=group_id, department_id=department_id, priority=priority,
            is_active=is_active_bool, created_by_id=current_user.id,
        )
        db.add(rule)
    else:
        old = {"department_id": rule.department_id, "priority": rule.priority, "is_active": rule.is_active}
        rule.department_id = department_id
        rule.priority = priority
        rule.is_active = is_active_bool
        rule.updated_at = utcnow()
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="ad_department_rule.set", object_type="ad_department_rule",
        object_id=rule.id, old_value=old,
        new_value={"department_id": department_id, "priority": priority, "is_active": is_active_bool},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/ad-groups", msg="Правило сохранено")


@router.get("/department-rules")
def admin_department_rules_dry_run(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    changes = plan_ad_department_sync(db)
    departments = {d.id: d.name for d in db.query(Department).all()}
    return templates.TemplateResponse(
        "admin/department_rules.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "changes": changes, "departments": departments,
        },
    )


@router.post("/department-rules/apply", dependencies=[Depends(require_csrf)])
def admin_department_rules_apply(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    changes = apply_ad_department_sync(db)
    audit_record(
        db, actor_app_user_id=current_user.id, action="ad_department_rules.apply",
        object_type="ad_department_rule", new_value={"changed_users": len(changes)},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/department-rules", msg=f"Применено изменений: {len(changes)}")


# ---------------------------------------------------------------------------
# Принтеры и очереди
# ---------------------------------------------------------------------------


@router.get("/printers")
def admin_printers(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    queues = db.query(PrinterQueue).order_by(PrinterQueue.is_active.desc(), PrinterQueue.printer_name).all()
    devices = db.query(PrinterDevice).order_by(PrinterDevice.is_active.desc(), PrinterDevice.display_name).all()
    links = db.query(PrinterDeviceQueueLink).filter_by(is_active=True).all()
    device_links = {}
    for link in links:
        device_links.setdefault(link.printer_device_id, []).append(link)
    return templates.TemplateResponse(
        "admin/printers.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "queues": queues, "devices": devices, "device_links": device_links,
            "sites": (
                db.query(Site)
                .filter_by(site_code=get_settings().site_code, is_active=True)
                .order_by(Site.name)
                .all()
                if get_agent_settings().mode != "central"
                else []
            ),
            "allow_device_provisioning": get_agent_settings().mode != "central",
            "snmp_profiles": db.query(SnmpProfile).filter_by(is_active=True).order_by(SnmpProfile.name).all(),
        },
    )


def _printer_queue_site_id(queue: PrinterQueue) -> Optional[int]:
    if queue.print_server is not None:
        return queue.print_server.site_id
    if queue.endpoint_agent is not None:
        return queue.endpoint_agent.site_id
    return None


@router.post("/printer-devices/create", dependencies=[Depends(require_csrf)])
def admin_printer_device_create(
    request: Request,
    site_id: int = Form(...),
    display_name: str = Form(...),
    ip_address: str = Form(""),
    hostname: str = Form(""),
    vendor: str = Form(""),
    model: str = Form(""),
    monitoring_source: Literal["disabled", "direct_snmp", "zabbix_api", "manual"] = Form("disabled"),
    snmp_profile_id: Optional[int] = Form(None),
    zabbix_host_id: str = Form(""),
    queue_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    if get_agent_settings().mode == "central":
        return _redirect(
            "/admin/printers",
            err="Устройство нужно создать на сервере его площадки: центр получает данные, но не отправляет конфигурацию агентам",
        )
    site = db.get(Site, site_id)
    if site is None or not site.is_active or site.site_code != get_settings().site_code:
        return _redirect("/admin/printers", err="Можно настраивать устройства только текущей локальной площадки")
    queue = db.get(PrinterQueue, queue_id) if queue_id else None
    if queue_id and queue is None:
        return _redirect("/admin/printers", err="Очередь печати не найдена")
    if queue is not None and _printer_queue_site_id(queue) not in (None, site.id):
        return _redirect("/admin/printers", err="Устройство и очередь должны находиться на одной площадке")
    if monitoring_source == "direct_snmp":
        profile = db.get(SnmpProfile, snmp_profile_id) if snmp_profile_id else None
        if profile is None or not profile.is_active:
            return _redirect("/admin/printers", err="Для прямого SNMP выберите активный профиль")
        if not ip_address.strip():
            return _redirect("/admin/printers", err="Для прямого SNMP укажите IP-адрес")
    if monitoring_source == "zabbix_api" and not zabbix_host_id.strip():
        return _redirect("/admin/printers", err="Для Zabbix укажите ID хоста")
    try:
        device = create_device(
            db, actor=current_user, site_id=site.id, display_name=display_name,
            ip_address=ip_address.strip() or None, hostname=hostname.strip() or None,
            vendor=vendor.strip() or None, model=model.strip() or None,
            print_server_id=queue.print_server_id if queue is not None else None,
        )
        set_monitoring_source(
            db, actor=current_user, device=device, source=monitoring_source,
            snmp_profile_id=snmp_profile_id if monitoring_source == "direct_snmp" else None,
            zabbix_host_id=zabbix_host_id.strip() or None,
        )
        if queue is not None:
            link_queue(db, actor=current_user, device=device, queue=queue)
        db.commit()
    except DeviceActionError as exc:
        db.rollback()
        return _redirect("/admin/printers", err=str(exc))
    return _redirect("/admin/printers", msg=f"Устройство «{device.display_name}» добавлено")


@router.post("/printer-devices/{device_id}/source", dependencies=[Depends(require_csrf)])
def admin_printer_device_source(
    device_id: int,
    request: Request,
    monitoring_source: Literal["disabled", "direct_snmp", "zabbix_api", "manual"] = Form(...),
    snmp_profile_id: Optional[int] = Form(None),
    zabbix_host_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    if get_agent_settings().mode == "central":
        return _redirect("/admin/printers", err="Источник мониторинга меняется на сервере площадки")
    device = db.get(PrinterDevice, device_id)
    if device is None:
        return _redirect("/admin/printers", err="Устройство не найдено")
    if monitoring_source == "direct_snmp":
        profile = db.get(SnmpProfile, snmp_profile_id) if snmp_profile_id else None
        if profile is None or not profile.is_active:
            return _redirect("/admin/printers", err="Для прямого SNMP выберите активный профиль")
        if not device.ip_address:
            return _redirect("/admin/printers", err="У устройства нет IP-адреса")
    if monitoring_source == "zabbix_api" and not zabbix_host_id.strip():
        return _redirect("/admin/printers", err="Для Zabbix укажите ID хоста")
    try:
        set_monitoring_source(
            db, actor=current_user, device=device, source=monitoring_source,
            snmp_profile_id=snmp_profile_id if monitoring_source == "direct_snmp" else None,
            zabbix_host_id=zabbix_host_id.strip() or None,
        )
        db.commit()
    except DeviceActionError as exc:
        db.rollback()
        return _redirect("/admin/printers", err=str(exc))
    return _redirect("/admin/printers", msg=f"Источник мониторинга «{device.display_name}» обновлён")


@router.post("/printer-devices/{device_id}/link", dependencies=[Depends(require_csrf)])
def admin_printer_device_link(
    device_id: int,
    request: Request,
    queue_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    if get_agent_settings().mode == "central":
        return _redirect("/admin/printers", err="Связи очередей меняются на сервере площадки")
    device = db.get(PrinterDevice, device_id)
    queue = db.get(PrinterQueue, queue_id)
    if device is None or queue is None:
        return _redirect("/admin/printers", err="Устройство или очередь не найдены")
    if _printer_queue_site_id(queue) not in (None, device.site_id):
        return _redirect("/admin/printers", err="Устройство и очередь должны находиться на одной площадке")
    try:
        link_queue(db, actor=current_user, device=device, queue=queue)
        db.commit()
    except DeviceActionError as exc:
        db.rollback()
        return _redirect("/admin/printers", err=str(exc))
    return _redirect("/admin/printers", msg="Очередь связана с физическим устройством")


@router.post("/printer-devices/links/{link_id}/unlink", dependencies=[Depends(require_csrf)])
def admin_printer_device_unlink(
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    if get_agent_settings().mode == "central":
        return _redirect("/admin/printers", err="Связи очередей меняются на сервере площадки")
    link = db.get(PrinterDeviceQueueLink, link_id)
    if link is None:
        return _redirect("/admin/printers", err="Связь не найдена")
    try:
        unlink_queue(db, actor=current_user, link=link)
        db.commit()
    except DeviceActionError as exc:
        db.rollback()
        return _redirect("/admin/printers", err=str(exc))
    return _redirect("/admin/printers", msg="Связь с очередью отключена")


@router.post("/printers/discover", dependencies=[Depends(require_csrf)])
def admin_printers_discover(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    started = utcnow()
    print_server = get_or_create_local_print_server(db)
    try:
        summary = sync_printer_queues(db, print_server_id=print_server.id)
    except PrinterDiscoveryError as exc:
        db.rollback()
        db.add(
            SyncRun(
                run_type="printer_discovery", started_at=started, finished_at=utcnow(),
                status="failed", error_message=str(exc)[:2000],
            )
        )
        db.commit()
        return _redirect("/admin/printers", err=safe_error_message(exc, "обнаружение очередей печати"))

    db.add(
        SyncRun(
            run_type="printer_discovery", started_at=started, finished_at=utcnow(), status="success",
            inserted=summary.created, events_fetched=summary.seen, skipped=summary.newly_missing,
        )
    )
    audit_record(
        db, actor_app_user_id=current_user.id, action="printer_queue.discover", object_type="printer_queue",
        new_value={"created": summary.created, "missing": summary.newly_missing, "seen": summary.seen},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect(
        "/admin/printers",
        msg=f"Найдено очередей: {summary.seen}, новых: {summary.created}, пропало: {summary.newly_missing}",
    )


@router.post("/printers/{queue_id}/update", dependencies=[Depends(require_csrf)])
def admin_printers_update(
    queue_id: int, request: Request,
    display_name: str = Form(""), color_mode: Literal["unknown", "bw", "color"] = Form("unknown"),
    collection_enabled: Optional[str] = Form(None), price_per_page: Optional[float] = Form(None), currency: str = Form("KZT"),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    # Как и в /admin/departments: чекбокс не отправляется вовсе, когда снят,
    # поэтому bool с дефолтом True сделал бы отключение учёта через форму
    # невозможным -- принимаем строку и сравниваем явно.
    queue = db.get(PrinterQueue, queue_id)
    if queue is None:
        return _redirect("/admin/printers", err="Очередь не найдена")
    old = {
        "display_name": queue.display_name, "color_mode": queue.color_mode,
        "collection_enabled": queue.collection_enabled, "price_per_page": queue.price_per_page,
    }
    queue.display_name = display_name.strip() or queue.printer_name
    queue.color_mode = color_mode
    queue.collection_enabled = collection_enabled == "true"
    queue.price_per_page = price_per_page
    queue.currency = currency or queue.currency
    queue.updated_at = utcnow()
    audit_record(
        db, actor_app_user_id=current_user.id, action="printer_queue.update", object_type="printer_queue",
        object_id=queue.id, old_value=old,
        new_value={
            "display_name": queue.display_name, "color_mode": color_mode,
            "collection_enabled": queue.collection_enabled, "price_per_page": price_per_page,
        },
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/printers", msg=f"Очередь «{queue.printer_name}» обновлена")


# ---------------------------------------------------------------------------
# Тарифы
# ---------------------------------------------------------------------------


@router.get("/pricing")
def admin_pricing(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    rules = db.query(PriceRule).order_by(PriceRule.priority.desc(), PriceRule.id.desc()).all()
    queues = db.query(PrinterQueue).order_by(PrinterQueue.printer_name).all()
    return templates.TemplateResponse(
        "admin/pricing.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rules": rules, "queues": queues,
        },
    )


@router.post("/pricing/create", dependencies=[Depends(require_csrf)])
def admin_pricing_create(
    request: Request,
    printer_queue_id: Optional[int] = Form(None), is_color: bool = Form(False),
    price_per_page: float = Form(...), currency: str = Form("KZT"),
    valid_from: str = Form(""), valid_to: str = Form(""), priority: int = Form(0),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    if price_per_page < 0:
        return _redirect("/admin/pricing", err="Цена за страницу не может быть отрицательной")
    rule = PriceRule(
        printer_queue_id=printer_queue_id or None, is_color=is_color, price_per_page=price_per_page,
        currency=currency or "KZT", valid_from=_parse_date_or_none(valid_from), valid_to=_parse_date_or_none(valid_to),
        priority=priority, is_active=True, created_by_id=current_user.id,
    )
    db.add(rule)
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="price_rule.create", object_type="price_rule",
        object_id=rule.id, new_value={"price_per_page": price_per_page, "printer_queue_id": printer_queue_id},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/pricing", msg="Тариф создан")


@router.post("/pricing/{rule_id}/deactivate", dependencies=[Depends(require_csrf)])
def admin_pricing_deactivate(
    rule_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    rule = db.get(PriceRule, rule_id)
    if rule is None:
        return _redirect("/admin/pricing", err="Тариф не найден")
    rule.is_active = False
    rule.updated_at = utcnow()
    audit_record(
        db, actor_app_user_id=current_user.id, action="price_rule.deactivate", object_type="price_rule",
        object_id=rule.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/pricing", msg="Тариф отключён")


@router.get("/pricing/test")
def admin_pricing_test(
    printer_queue_id: int, pages: int = 1, at: str = "",
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    queue = db.get(PrinterQueue, printer_queue_id)
    if queue is None:
        return {"error": "Очередь не найдена"}
    at_dt = _parse_date_or_none(at) or utcnow()
    resolution = resolve_price(db, queue, at_dt, get_settings())
    return {
        "printer_queue": queue.printer_name, "price_per_page": resolution.price_per_page,
        "is_color": resolution.is_color, "color_source": resolution.color_source,
        "currency": resolution.currency, "price_rule_id": resolution.price_rule_id,
        "pages": pages, "total_cost": round(resolution.price_per_page * pages, 2),
    }


# ---------------------------------------------------------------------------
# Площадки и центральный менеджер Print Server (см. docs/MULTISITE_ARCHITECTURE.md)
# ---------------------------------------------------------------------------


@router.get("/sites")
def admin_sites(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    sites = db.query(Site).order_by(Site.name).all()
    return templates.TemplateResponse(
        "admin/sites.html",
        {"request": request, "current_user": current_user, "csrf_token": csrf_token(request), "sites": sites},
    )


@router.post("/sites/create", dependencies=[Depends(require_csrf)])
def admin_sites_create(
    request: Request,
    site_code: str = Form(...), name: str = Form(""), description: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    site_code = site_code.strip()
    if not site_code:
        return _redirect("/admin/sites", err="site_code обязателен")
    if db.query(Site).filter_by(site_code=site_code).first():
        return _redirect("/admin/sites", err=f"Площадка с site_code «{site_code}» уже существует")
    site = Site(site_code=site_code, name=(name.strip() or site_code), description=description or None, is_active=True)
    db.add(site)
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="site.create", object_type="site",
        object_id=site.id, new_value={"site_code": site_code}, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/sites", msg=f"Площадка «{site.name}» создана")


@router.post("/sites/{site_id}/update", dependencies=[Depends(require_csrf)])
def admin_sites_update(
    site_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    site = db.get(Site, site_id)
    if site is None:
        return _redirect("/admin/sites", err="Площадка не найдена")
    name = name.strip()
    if not name:
        return _redirect("/admin/sites", err="Название площадки обязательно")
    old = {"name": site.name, "description": site.description}
    site.name = name
    site.description = description.strip() or None
    audit_record(
        db, actor_app_user_id=current_user.id, action="site.update", object_type="site",
        object_id=site.id, old_value=old,
        new_value={"name": site.name, "description": site.description},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/sites", msg=f"Площадка «{site.name}» обновлена")


@router.get("/print-servers")
def admin_print_servers(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    servers = db.query(PrintServer).order_by(PrintServer.site_id, PrintServer.server_name).all()
    today_start = naive_utc(utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for server in servers:
        jobs_today = (
            db.query(func.count(PrintJob.id))
            .filter(PrintJob.print_server_id == server.id, PrintJob.time_created >= today_start)
            .scalar()
            or 0
        )
        printer_count = (
            db.query(func.count(PrinterQueue.id))
            .filter(PrinterQueue.print_server_id == server.id, PrinterQueue.is_active.is_(True))
            .scalar()
            or 0
        )
        rows.append(
            {
                "server": server,
                "status": compute_status(server),
                "jobs_today": jobs_today,
                "printer_count": printer_count,
                "has_token": bool(server.token_hash),
            }
        )
    sites = db.query(Site).filter_by(is_active=True).order_by(Site.name).all()
    return templates.TemplateResponse(
        "admin/print_servers.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "sites": sites,
        },
    )


@router.post("/print-servers/create", dependencies=[Depends(require_csrf)])
def admin_print_servers_create(
    request: Request,
    site_id: int = Form(...), server_name: str = Form(...), display_name: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    site = db.get(Site, site_id)
    if site is None:
        return _redirect("/admin/print-servers", err="Площадка не найдена")
    server_name = server_name.strip()
    if not server_name:
        return _redirect("/admin/print-servers", err="Имя сервера обязательно")
    if db.query(PrintServer).filter_by(site_id=site_id, server_name=server_name).first():
        return _redirect("/admin/print-servers", err=f"Сервер «{server_name}» уже зарегистрирован на этой площадке")

    raw_token = generate_agent_token()
    server = PrintServer(
        site_id=site_id, server_name=server_name, display_name=display_name.strip() or server_name,
        token_hash=hash_agent_token(raw_token), token_created_at=utcnow(),
    )
    db.add(server)
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="print_server.create", object_type="print_server",
        object_id=server.id, new_value={"site_id": site_id, "server_name": server_name},
        ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect(
        "/admin/print-servers",
        msg=(
            f"Print Server «{server_name}» зарегистрирован. Токен агента (показывается один раз, "
            f"скопируйте в AGENT_TOKEN в .env агента): {raw_token}"
        ),
    )


@router.post("/print-servers/{server_id}/rotate-token", dependencies=[Depends(require_csrf)])
def admin_print_servers_rotate_token(
    server_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    server = db.get(PrintServer, server_id)
    if server is None:
        return _redirect("/admin/print-servers", err="Print Server не найден")
    raw_token = generate_agent_token()
    server.token_hash = hash_agent_token(raw_token)
    server.token_rotated_at = utcnow()
    audit_record(
        db, actor_app_user_id=current_user.id, action="print_server.rotate_token", object_type="print_server",
        object_id=server.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect(
        "/admin/print-servers",
        msg=(
            f"Токен для «{server.server_name}» перевыпущен (старый немедленно недействителен). "
            f"Новый токен (показывается один раз): {raw_token}"
        ),
    )


@router.post("/print-servers/{server_id}/disable", dependencies=[Depends(require_csrf)])
def admin_print_servers_disable(
    server_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    server = db.get(PrintServer, server_id)
    if server is None:
        return _redirect("/admin/print-servers", err="Print Server не найден")
    server.is_disabled = True
    audit_record(
        db, actor_app_user_id=current_user.id, action="print_server.disable", object_type="print_server",
        object_id=server.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/print-servers", msg=f"Print Server «{server.server_name}» отключён — токен больше не принимается")


@router.post("/print-servers/{server_id}/enable", dependencies=[Depends(require_csrf)])
def admin_print_servers_enable(
    server_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    server = db.get(PrintServer, server_id)
    if server is None:
        return _redirect("/admin/print-servers", err="Print Server не найден")
    server.is_disabled = False
    audit_record(
        db, actor_app_user_id=current_user.id, action="print_server.enable", object_type="print_server",
        object_id=server.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/print-servers", msg=f"Print Server «{server.server_name}» включён")


# ---------------------------------------------------------------------------
# Endpoint-агенты (USB/прямая печать на пользовательских ПК) — ЛОКАЛЬНЫЙ
# раздел площадки (standalone/agent), не завязан на APP_MODE=central, см.
# webapp/endpoint_api.py и docs/PRINTER_MONITORING_FORECASTING.md.
# ---------------------------------------------------------------------------


@router.get("/endpoint-agents")
def admin_endpoint_agents(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES))
):
    agents = db.query(EndpointAgent).order_by(EndpointAgent.hostname).all()
    today_start = naive_utc(utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for agent in agents:
        jobs_today = (
            db.query(func.count(PrintJob.id))
            .filter(PrintJob.endpoint_agent_id == agent.id, PrintJob.time_created >= today_start)
            .scalar()
            or 0
        )
        rows.append({"agent": agent, "status": compute_status(agent), "jobs_today": jobs_today})
    return templates.TemplateResponse(
        "admin/endpoint_agents.html",
        {"request": request, "current_user": current_user, "csrf_token": csrf_token(request), "rows": rows},
    )


@router.post("/endpoint-agents/create", dependencies=[Depends(require_csrf)])
def admin_endpoint_agents_create(
    request: Request,
    hostname: str = Form(...), display_name: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    hostname = hostname.strip()
    if not hostname:
        return _redirect("/admin/endpoint-agents", err="Имя компьютера обязательно")

    settings = get_settings()
    site = db.query(Site).filter_by(site_code=settings.site_code).first()
    if site is None:
        return _redirect(
            "/admin/endpoint-agents",
            err="Локальная площадка ещё не создана — запустите сборщик хотя бы раз (он заводит её сам).",
        )
    if db.query(EndpointAgent).filter_by(site_id=site.id, hostname=hostname).first():
        return _redirect("/admin/endpoint-agents", err=f"Endpoint-агент «{hostname}» уже зарегистрирован")

    raw_token = generate_agent_token()
    agent = EndpointAgent(
        site_id=site.id, hostname=hostname, display_name=display_name.strip() or hostname,
        token_hash=hash_agent_token(raw_token), token_created_at=utcnow(),
    )
    db.add(agent)
    db.flush()
    audit_record(
        db, actor_app_user_id=current_user.id, action="endpoint_agent.create", object_type="endpoint_agent",
        object_id=agent.id, new_value={"hostname": hostname}, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect(
        "/admin/endpoint-agents",
        msg=(
            f"Endpoint-агент «{hostname}» зарегистрирован. ENDPOINT_UUID={agent.uuid} "
            f"ENDPOINT_TOKEN={raw_token} (токен показывается один раз — скопируйте обе строки "
            f"в endpoint_agent.env на этом ПК)."
        ),
    )


@router.post("/endpoint-agents/{agent_id}/rotate-token", dependencies=[Depends(require_csrf)])
def admin_endpoint_agents_rotate_token(
    agent_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    agent = db.get(EndpointAgent, agent_id)
    if agent is None:
        return _redirect("/admin/endpoint-agents", err="Endpoint-агент не найден")
    raw_token = generate_agent_token()
    agent.token_hash = hash_agent_token(raw_token)
    agent.token_rotated_at = utcnow()
    audit_record(
        db, actor_app_user_id=current_user.id, action="endpoint_agent.rotate_token", object_type="endpoint_agent",
        object_id=agent.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect(
        "/admin/endpoint-agents",
        msg=f"Токен для «{agent.hostname}» перевыпущен (старый недействителен). Новый (один раз): {raw_token}",
    )


@router.post("/endpoint-agents/{agent_id}/disable", dependencies=[Depends(require_csrf)])
def admin_endpoint_agents_disable(
    agent_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    agent = db.get(EndpointAgent, agent_id)
    if agent is None:
        return _redirect("/admin/endpoint-agents", err="Endpoint-агент не найден")
    agent.is_disabled = True
    audit_record(
        db, actor_app_user_id=current_user.id, action="endpoint_agent.disable", object_type="endpoint_agent",
        object_id=agent.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/endpoint-agents", msg=f"«{agent.hostname}» отключён — токен больше не принимается")


@router.post("/endpoint-agents/{agent_id}/enable", dependencies=[Depends(require_csrf)])
def admin_endpoint_agents_enable(
    agent_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    agent = db.get(EndpointAgent, agent_id)
    if agent is None:
        return _redirect("/admin/endpoint-agents", err="Endpoint-агент не найден")
    agent.is_disabled = False
    audit_record(
        db, actor_app_user_id=current_user.id, action="endpoint_agent.enable", object_type="endpoint_agent",
        object_id=agent.id, ip_address=get_client_ip(request),
    )
    db.commit()
    return _redirect("/admin/endpoint-agents", msg=f"«{agent.hostname}» включён")


# ---------------------------------------------------------------------------
# Профили SNMP (см. printaudit.monitoring.snmp_adapter, docs/PRINTER_MONITORING_FORECASTING.md)
# ---------------------------------------------------------------------------


def _snmp_profile_form_kwargs(
    name: str, description: str, snmp_version: str, port: int, timeout_seconds: float, retries: int,
    oid_map_json: str, credentials_env_var: str, snmp_v3_username: str, snmp_v3_auth_protocol: str,
    snmp_v3_auth_key_env_var: str, snmp_v3_priv_protocol: str, snmp_v3_priv_key_env_var: str,
) -> dict:
    return dict(
        name=name, description=description, snmp_version=snmp_version, port=port, timeout_seconds=timeout_seconds,
        retries=retries, oid_map_json=oid_map_json, credentials_env_var=credentials_env_var or None,
        snmp_v3_username=snmp_v3_username or None, snmp_v3_auth_protocol=snmp_v3_auth_protocol or None,
        snmp_v3_auth_key_env_var=snmp_v3_auth_key_env_var or None, snmp_v3_priv_protocol=snmp_v3_priv_protocol or None,
        snmp_v3_priv_key_env_var=snmp_v3_priv_key_env_var or None,
    )


@router.get("/snmp-profiles")
def admin_snmp_profiles(
    request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    profiles = db.query(SnmpProfile).order_by(SnmpProfile.name).all()
    return templates.TemplateResponse(
        "admin/snmp_profiles.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "profiles": profiles,
        },
    )


@router.post("/snmp-profiles/create", dependencies=[Depends(require_csrf)])
def admin_snmp_profiles_create(
    request: Request,
    name: str = Form(...), description: str = Form(""), snmp_version: str = Form("v3"),
    port: int = Form(161), timeout_seconds: float = Form(2.0), retries: int = Form(1),
    oid_map_json: str = Form("{}"), credentials_env_var: str = Form(""),
    snmp_v3_username: str = Form(""), snmp_v3_auth_protocol: str = Form(""),
    snmp_v3_auth_key_env_var: str = Form(""), snmp_v3_priv_protocol: str = Form(""),
    snmp_v3_priv_key_env_var: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    kwargs = _snmp_profile_form_kwargs(
        name, description, snmp_version, port, timeout_seconds, retries, oid_map_json, credentials_env_var,
        snmp_v3_username, snmp_v3_auth_protocol, snmp_v3_auth_key_env_var, snmp_v3_priv_protocol,
        snmp_v3_priv_key_env_var,
    )
    try:
        profile = create_snmp_profile(db, actor=current_user, **kwargs)
    except SnmpProfileError as exc:
        db.rollback()
        return _redirect("/admin/snmp-profiles", err=str(exc))
    db.commit()
    return _redirect("/admin/snmp-profiles", msg=f"Профиль SNMP «{profile.name}» создан")


@router.post("/snmp-profiles/{profile_id}/update", dependencies=[Depends(require_csrf)])
def admin_snmp_profiles_update(
    profile_id: int, request: Request,
    name: str = Form(...), description: str = Form(""), snmp_version: str = Form("v3"),
    port: int = Form(161), timeout_seconds: float = Form(2.0), retries: int = Form(1),
    oid_map_json: str = Form("{}"), credentials_env_var: str = Form(""),
    snmp_v3_username: str = Form(""), snmp_v3_auth_protocol: str = Form(""),
    snmp_v3_auth_key_env_var: str = Form(""), snmp_v3_priv_protocol: str = Form(""),
    snmp_v3_priv_key_env_var: str = Form(""),
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    profile = db.get(SnmpProfile, profile_id)
    if profile is None:
        return _redirect("/admin/snmp-profiles", err="Профиль SNMP не найден")
    kwargs = _snmp_profile_form_kwargs(
        name, description, snmp_version, port, timeout_seconds, retries, oid_map_json, credentials_env_var,
        snmp_v3_username, snmp_v3_auth_protocol, snmp_v3_auth_key_env_var, snmp_v3_priv_protocol,
        snmp_v3_priv_key_env_var,
    )
    try:
        update_snmp_profile(db, actor=current_user, profile=profile, **kwargs)
    except SnmpProfileError as exc:
        db.rollback()
        return _redirect("/admin/snmp-profiles", err=str(exc))
    db.commit()
    return _redirect("/admin/snmp-profiles", msg=f"Профиль SNMP «{profile.name}» обновлён")


@router.post("/snmp-profiles/{profile_id}/disable", dependencies=[Depends(require_csrf)])
def admin_snmp_profiles_disable(
    profile_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    profile = db.get(SnmpProfile, profile_id)
    if profile is None:
        return _redirect("/admin/snmp-profiles", err="Профиль SNMP не найден")
    set_snmp_profile_active(db, actor=current_user, profile=profile, is_active=False)
    db.commit()
    return _redirect("/admin/snmp-profiles", msg=f"Профиль SNMP «{profile.name}» отключён")


@router.post("/snmp-profiles/{profile_id}/enable", dependencies=[Depends(require_csrf)])
def admin_snmp_profiles_enable(
    profile_id: int, request: Request,
    db: Session = Depends(get_db), current_user: AppUser = Depends(require_role(*ADMIN_ROLES)),
):
    profile = db.get(SnmpProfile, profile_id)
    if profile is None:
        return _redirect("/admin/snmp-profiles", err="Профиль SNMP не найден")
    set_snmp_profile_active(db, actor=current_user, profile=profile, is_active=True)
    db.commit()
    return _redirect("/admin/snmp-profiles", msg=f"Профиль SNMP «{profile.name}» включён")
