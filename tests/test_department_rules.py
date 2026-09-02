"""Тесты правил 'AD-группа -> отдел': приоритет при конфликте, ручной
override (locked), dry-run/apply синхронизации, и резолвинг отдела для
задания печати с fallback на легаси-таблицу users."""


def _make_department(session, Department, name):
    d = Department(name=name)
    session.add(d)
    session.flush()
    return d


def _make_group(session, AdGroup, dn, name):
    g = AdGroup(dn=dn, sam_account_name=name, display_name=name)
    session.add(g)
    session.flush()
    return g


def _make_ad_user(session, AdUser, login_normalized, **kwargs):
    u = AdUser(
        sam_account_name=kwargs.pop("sam_account_name", login_normalized.split("\\")[-1]),
        login_normalized=login_normalized,
        display_name=kwargs.pop("display_name", login_normalized),
        **kwargs,
    )
    session.add(u)
    session.flush()
    return u


def test_single_matching_rule_resolves_department(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import resolve_department_for_ad_user
    from printaudit.models import AdDepartmentRule, AdGroup, AdGroupMembership, AdUser, Department

    session = SessionLocal()
    dept = _make_department(session, Department, "Бухгалтерия")
    group = _make_group(session, AdGroup, "cn=accounting,dc=x", "accounting")
    session.add(AdDepartmentRule(ad_group_id=group.id, department_id=dept.id, priority=0))
    ad_user = _make_ad_user(session, AdUser, "domain\\ivanov")
    session.flush()
    session.add(AdGroupMembership(ad_group_id=group.id, ad_user_id=ad_user.id))
    session.commit()

    resolution = resolve_department_for_ad_user(session, ad_user)
    assert resolution.department_id == dept.id
    assert resolution.source == "group_rule"
    assert resolution.conflicting_rule_ids == []
    session.close()


def test_higher_priority_rule_wins_when_user_in_two_groups(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import resolve_department_for_ad_user
    from printaudit.models import AdDepartmentRule, AdGroup, AdGroupMembership, AdUser, Department

    session = SessionLocal()
    dept_low = _make_department(session, Department, "Продажи")
    dept_high = _make_department(session, Department, "Финансы")
    group_low = _make_group(session, AdGroup, "cn=sales,dc=x", "sales")
    group_high = _make_group(session, AdGroup, "cn=finance,dc=x", "finance")
    session.add(AdDepartmentRule(ad_group_id=group_low.id, department_id=dept_low.id, priority=0))
    session.add(AdDepartmentRule(ad_group_id=group_high.id, department_id=dept_high.id, priority=10))
    ad_user = _make_ad_user(session, AdUser, "domain\\petrova")
    session.flush()
    session.add(AdGroupMembership(ad_group_id=group_low.id, ad_user_id=ad_user.id))
    session.add(AdGroupMembership(ad_group_id=group_high.id, ad_user_id=ad_user.id))
    session.commit()

    resolution = resolve_department_for_ad_user(session, ad_user)
    assert resolution.department_id == dept_high.id
    assert resolution.conflicting_rule_ids == []
    session.close()


def test_equal_priority_conflict_is_reported_not_hidden(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import resolve_department_for_ad_user
    from printaudit.models import AdDepartmentRule, AdGroup, AdGroupMembership, AdUser, Department

    session = SessionLocal()
    dept_a = _make_department(session, Department, "Отдел А")
    dept_b = _make_department(session, Department, "Отдел Б")
    group_a = _make_group(session, AdGroup, "cn=a,dc=x", "a")
    group_b = _make_group(session, AdGroup, "cn=b,dc=x", "b")
    rule_a = AdDepartmentRule(ad_group_id=group_a.id, department_id=dept_a.id, priority=5)
    rule_b = AdDepartmentRule(ad_group_id=group_b.id, department_id=dept_b.id, priority=5)
    session.add_all([rule_a, rule_b])
    ad_user = _make_ad_user(session, AdUser, "domain\\smirnov")
    session.flush()
    session.add(AdGroupMembership(ad_group_id=group_a.id, ad_user_id=ad_user.id))
    session.add(AdGroupMembership(ad_group_id=group_b.id, ad_user_id=ad_user.id))
    session.commit()

    resolution = resolve_department_for_ad_user(session, ad_user)
    # Детерминированный выбор (меньший id правила), но конфликт обязан быть виден.
    assert resolution.department_id in (dept_a.id, dept_b.id)
    assert resolution.conflicting_rule_ids != []
    session.close()


def test_locked_department_is_never_overwritten_by_rule(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import plan_ad_department_sync
    from printaudit.models import AdDepartmentRule, AdGroup, AdGroupMembership, AdUser, Department

    session = SessionLocal()
    dept_manual = _make_department(session, Department, "Ручной отдел")
    dept_rule = _make_department(session, Department, "Отдел по правилу")
    group = _make_group(session, AdGroup, "cn=g,dc=x", "g")
    session.add(AdDepartmentRule(ad_group_id=group.id, department_id=dept_rule.id, priority=0))
    ad_user = _make_ad_user(
        session, AdUser, "domain\\locked_user",
        department_id=dept_manual.id, department_source="manual", department_locked=True,
    )
    session.flush()
    session.add(AdGroupMembership(ad_group_id=group.id, ad_user_id=ad_user.id))
    session.commit()

    changes = plan_ad_department_sync(session)
    assert changes == []  # locked-пользователь не должен появляться как "изменение"

    session.refresh(ad_user)
    assert ad_user.department_id == dept_manual.id
    session.close()


def test_apply_sync_updates_unlocked_users_and_reports_changes(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import apply_ad_department_sync
    from printaudit.models import AdDepartmentRule, AdGroup, AdGroupMembership, AdUser, Department

    session = SessionLocal()
    dept = _make_department(session, Department, "IT")
    group = _make_group(session, AdGroup, "cn=it,dc=x", "it")
    session.add(AdDepartmentRule(ad_group_id=group.id, department_id=dept.id, priority=0))
    ad_user = _make_ad_user(session, AdUser, "domain\\newbie", department_id=None)
    session.flush()
    session.add(AdGroupMembership(ad_group_id=group.id, ad_user_id=ad_user.id))
    session.commit()

    changes = apply_ad_department_sync(session)
    session.commit()

    assert len(changes) == 1
    assert changes[0].new_department_id == dept.id
    session.refresh(ad_user)
    assert ad_user.department_id == dept.id
    assert ad_user.department_source == "group_rule"
    session.close()


def test_lookup_department_for_print_job_uses_ad_user_first(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import lookup_department_for_print_job_user
    from printaudit.models import AdUser, Department

    session = SessionLocal()
    dept = _make_department(session, Department, "Отдел АД")
    _make_ad_user(session, AdUser, "domain\\ivanov", department_id=dept.id)
    session.commit()

    result = lookup_department_for_print_job_user(session, "DOMAIN\\ivanov")
    assert result == dept.id
    session.close()


def test_lookup_department_falls_back_to_legacy_users_table(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import lookup_department_for_print_job_user
    from printaudit.models import Department, User

    session = SessionLocal()
    dept = _make_department(session, Department, "Легаси отдел")
    session.add(User(user_name="DOMAIN\\legacyuser", department_id=dept.id))
    session.commit()

    result = lookup_department_for_print_job_user(session, "DOMAIN\\legacyuser")
    assert result == dept.id
    session.close()


def test_lookup_department_matches_bare_login_against_domain_qualified_ad_user(app_env):
    """Событие 307 иногда отдаёт логин без домена; ad_users хранит его с доменом."""
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import lookup_department_for_print_job_user
    from printaudit.models import AdUser, Department

    session = SessionLocal()
    dept = _make_department(session, Department, "Отдел")
    _make_ad_user(session, AdUser, "example.local\\ivanov", department_id=dept.id)
    session.commit()

    result = lookup_department_for_print_job_user(session, "ivanov", ad_domain="example.local")
    assert result == dept.id
    session.close()


def test_lookup_department_returns_none_when_no_match(app_env):
    from printaudit.database import SessionLocal
    from printaudit.department_resolver import lookup_department_for_print_job_user

    session = SessionLocal()
    result = lookup_department_for_print_job_user(session, "DOMAIN\\ghost")
    assert result is None
    session.close()
