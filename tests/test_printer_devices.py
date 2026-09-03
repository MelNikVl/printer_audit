"""Тесты PrinterDevice: создание, управляемая (не автоматическая) связь с
очередями с аудитом, вычисление статуса устройства по возрасту/
достижимости последнего сэмпла, классификация уровня расходника."""
from datetime import datetime, timedelta, timezone

import pytest


def _make_app_user(session, login="domain\\admin", role="admin"):
    from printaudit.models import AppUser

    user = AppUser(login_normalized=login, role=role, is_active=True)
    session.add(user)
    session.flush()
    return user


def test_create_device_requires_display_name(app_env):
    from printaudit.database import SessionLocal
    from printaudit.monitoring.devices import DeviceActionError, create_device
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="Site A")
    actor = _make_app_user(session)
    session.commit()

    with pytest.raises(DeviceActionError):
        create_device(session, actor=actor, site_id=site.id, display_name="   ")
    session.close()


def test_create_device_writes_audit_log(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AuditLog
    from printaudit.monitoring.devices import create_device
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="Site A")
    actor = _make_app_user(session)
    session.commit()

    device = create_device(session, actor=actor, site_id=site.id, display_name="HP LaserJet 3F")
    session.commit()

    entry = session.query(AuditLog).filter_by(action="printer_device.create").one()
    assert entry.object_id == str(device.id)
    session.close()


def test_link_and_unlink_queue_with_audit(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import AuditLog, PrinterQueue
    from printaudit.monitoring.devices import (
        DeviceActionError, create_device, get_active_queue_links, link_queue, unlink_queue,
    )
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="Site A")
    actor = _make_app_user(session)
    queue = PrinterQueue(printer_name="HP-3F-BW", display_name="HP-3F-BW")
    session.add(queue)
    session.commit()

    device = create_device(session, actor=actor, site_id=site.id, display_name="HP LaserJet 3F")
    session.commit()

    link = link_queue(session, actor=actor, device=device, queue=queue)
    session.commit()
    assert link.is_active is True
    assert len(get_active_queue_links(session, device)) == 1

    # Повторная попытка связать ту же (уже активную) пару — ошибка.
    with pytest.raises(DeviceActionError):
        link_queue(session, actor=actor, device=device, queue=queue)

    unlink_queue(session, actor=actor, link=link)
    session.commit()
    assert len(get_active_queue_links(session, device)) == 0

    actions = [a.action for a in session.query(AuditLog).order_by(AuditLog.id).all()]
    assert "printer_device.link_queue" in actions
    assert "printer_device.unlink_queue" in actions
    session.close()


def test_relink_after_unlink_reactivates_same_row(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterDeviceQueueLink, PrinterQueue
    from printaudit.monitoring.devices import create_device, link_queue, unlink_queue
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="Site A")
    actor = _make_app_user(session)
    queue = PrinterQueue(printer_name="HP-3F-BW", display_name="HP-3F-BW")
    session.add(queue)
    session.commit()
    device = create_device(session, actor=actor, site_id=site.id, display_name="HP LaserJet 3F")
    session.commit()

    link1 = link_queue(session, actor=actor, device=device, queue=queue)
    session.commit()
    unlink_queue(session, actor=actor, link=link1)
    session.commit()
    link2 = link_queue(session, actor=actor, device=device, queue=queue)
    session.commit()

    assert link1.id == link2.id  # переиспользуется та же строка, не дублируется
    assert session.query(PrinterDeviceQueueLink).count() == 1
    session.close()


def test_set_monitoring_source_rejects_unknown_value(app_env):
    from printaudit.database import SessionLocal
    from printaudit.monitoring.devices import DeviceActionError, create_device, set_monitoring_source
    from printaudit.sites import get_or_create_site

    session = SessionLocal()
    site = get_or_create_site(session, "SITE-A", name="Site A")
    actor = _make_app_user(session)
    session.commit()
    device = create_device(session, actor=actor, site_id=site.id, display_name="D1")
    session.commit()

    with pytest.raises(DeviceActionError):
        set_monitoring_source(session, actor=actor, device=device, source="carrier_pigeon")
    session.close()


def test_compute_device_status_no_sample_is_unknown():
    from printaudit.monitoring.status import compute_device_status

    assert compute_device_status(None) == "unknown"


def test_compute_device_status_stale_sample_is_offline():
    from printaudit.monitoring.status import compute_device_status

    class _Sample:
        collected_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        is_reachable = True
        device_status = "online"

    assert compute_device_status(_Sample()) == "offline"


def test_compute_device_status_unreachable_overrides_reported_status():
    from printaudit.monitoring.status import compute_device_status

    class _Sample:
        collected_at = datetime.now(timezone.utc).replace(tzinfo=None)
        is_reachable = False
        device_status = "online"  # источник противоречит is_reachable -- не доверяем

    assert compute_device_status(_Sample()) == "offline"


def test_compute_device_status_fresh_sample_passes_through():
    from printaudit.monitoring.status import compute_device_status

    class _Sample:
        collected_at = datetime.now(timezone.utc).replace(tzinfo=None)
        is_reachable = True
        device_status = "warning"

    assert compute_device_status(_Sample()) == "warning"


@pytest.mark.parametrize(
    "level,expected",
    [
        (None, "unknown"),
        (0, "empty"),
        (-1, "empty"),
        (3, "critical"),
        (5, "critical"),
        (10, "low"),
        (20, "low"),
        (21, "ok"),
        (100, "ok"),
    ],
)
def test_classify_supply_level(level, expected):
    from printaudit.monitoring import classify_supply_level

    assert classify_supply_level(level) == expected
