"""Разрешение отдела пользователя AD по правилам "AD-группа -> отдел".

Правила:
  - Если у AdUser выставлен department_locked=True — отдел НЕ трогаем вообще
    (это ручное назначение, admin явно защитил его от автосинхронизации).
  - Иначе смотрим на все активные AdDepartmentRule для групп, в которых
    состоит пользователь, и берём правило с наибольшим priority.
  - Если несколько активных правил делят наивысший priority И указывают на
    РАЗНЫЕ отделы — это конфликт: результат недетерминирован (берём тот, что
    с меньшим id правила, для стабильности), но конфликт обязательно
    возвращается вызывающему коду, чтобы он был виден в UI/логе, а не решался
    молча (см. AdRuleConflict в результате).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from printaudit.ad_normalize import normalize_login, with_domain
from printaudit.models import AdDepartmentRule, AdGroupMembership, AdUser, User


@dataclass
class Resolution:
    department_id: Optional[int]
    source: str  # "manual" | "group_rule" | "none"
    rule_id: Optional[int] = None
    conflicting_rule_ids: List[int] = field(default_factory=list)


def resolve_department_for_ad_user(session: Session, ad_user: AdUser) -> Resolution:
    if ad_user.department_locked:
        return Resolution(department_id=ad_user.department_id, source="manual")

    group_ids = [
        m.ad_group_id
        for m in session.query(AdGroupMembership).filter_by(ad_user_id=ad_user.id).all()
    ]
    if not group_ids:
        return Resolution(department_id=None, source="none")

    rules = (
        session.query(AdDepartmentRule)
        .filter(
            AdDepartmentRule.ad_group_id.in_(group_ids),
            AdDepartmentRule.is_active.is_(True),
        )
        .order_by(AdDepartmentRule.priority.desc(), AdDepartmentRule.id.asc())
        .all()
    )
    if not rules:
        return Resolution(department_id=None, source="none")

    winner = rules[0]
    tied = [
        r for r in rules[1:]
        if r.priority == winner.priority and r.department_id != winner.department_id
    ]
    return Resolution(
        department_id=winner.department_id,
        source="group_rule",
        rule_id=winner.id,
        conflicting_rule_ids=[r.id for r in tied],
    )


@dataclass
class PendingChange:
    ad_user_id: int
    login_normalized: str
    display_name: Optional[str]
    old_department_id: Optional[int]
    new_department_id: Optional[int]
    source: str
    rule_id: Optional[int]
    has_conflict: bool


def plan_ad_department_sync(session: Session) -> List[PendingChange]:
    """Считает, что бы изменилось при применении текущих правил AD-группа ->
    отдел, НИЧЕГО не записывая (dry-run для UI)."""
    changes: List[PendingChange] = []
    for ad_user in session.query(AdUser).filter_by(local_disabled=False).all():
        resolution = resolve_department_for_ad_user(session, ad_user)
        if resolution.source == "manual":
            continue  # ручные назначения синхронизация не трогает и не показывает как "изменение"
        if resolution.department_id != ad_user.department_id:
            changes.append(
                PendingChange(
                    ad_user_id=ad_user.id,
                    login_normalized=ad_user.login_normalized,
                    display_name=ad_user.display_name,
                    old_department_id=ad_user.department_id,
                    new_department_id=resolution.department_id,
                    source=resolution.source,
                    rule_id=resolution.rule_id,
                    has_conflict=bool(resolution.conflicting_rule_ids),
                )
            )
    return changes


def apply_ad_department_sync(session: Session) -> List[PendingChange]:
    """Применяет правила AD-группа -> отдел ко всем незаблокированным
    AdUser. Возвращает применённые изменения (для audit log / отчёта на
    странице групп). Коммитить должен вызывающий код."""
    changes = plan_ad_department_sync(session)
    now = datetime.now(timezone.utc)
    by_id = {c.ad_user_id: c for c in changes}
    if by_id:
        for ad_user in session.query(AdUser).filter(AdUser.id.in_(by_id.keys())).all():
            change = by_id[ad_user.id]
            ad_user.department_id = change.new_department_id
            ad_user.department_source = change.source
            ad_user.department_rule_id = change.rule_id
            ad_user.updated_at = now
    return changes


def lookup_department_for_print_job_user(
    session: Session, raw_user_name: str, ad_domain: Optional[str] = None
) -> Optional[int]:
    """Резолвинг отдела для ЗАДАНИЯ ПЕЧАТИ по user_name из события 307 (вызывается
    коллектором на каждое новое задание, не путать с apply_ad_department_sync,
    который обновляет ad_users.department_id по правилам групп заранее).

    Порядок: 1) точное совпадение по ad_users.login_normalized; 2) то же самое
    с подстановкой ad_domain, если сырой логин пришёл без домена;
    3) fallback на легаси-таблицу users (CSV-маппинг) по исходному user_name.
    """
    normalized = normalize_login(raw_user_name)
    ad_user = session.query(AdUser).filter_by(login_normalized=normalized).first()

    if ad_user is None and ad_domain:
        normalized_with_domain = with_domain(raw_user_name, ad_domain)
        if normalized_with_domain != normalized:
            ad_user = session.query(AdUser).filter_by(login_normalized=normalized_with_domain).first()

    if ad_user is not None and not ad_user.local_disabled:
        return ad_user.department_id

    legacy_user = session.get(User, raw_user_name)
    if legacy_user is not None:
        return legacy_user.department_id

    return None
