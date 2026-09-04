"""Управление физическими устройствами (PrinterDevice) и их связью с
очередями печати (PrinterDeviceQueueLink) — ВСЕГДА через явное действие
администратора, никогда автоматическим сопоставлением по имени/IP (см.
printaudit.models.PrinterDevice). Каждое изменение — запись в audit_log."""
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from printaudit import audit
from printaudit.models import AppUser, PrinterDevice, PrinterDeviceQueueLink, PrinterQueue
from printaudit.monitoring import MONITORING_SOURCE_DISABLED, MONITORING_SOURCES
from printaudit.timeutil import utcnow


class DeviceActionError(Exception):
    """Ожидаемая ошибка бизнес-правила — показывается пользователю как
    есть, не как внутренняя ошибка 500 (тот же принцип, что и
    printaudit.admin_users.AdminActionError)."""


def create_device(
    session: Session,
    *,
    actor: AppUser,
    site_id: int,
    display_name: str,
    hostname: Optional[str] = None,
    ip_address: Optional[str] = None,
    mac_address: Optional[str] = None,
    serial_number: Optional[str] = None,
    vendor: Optional[str] = None,
    model: Optional[str] = None,
    print_server_id: Optional[int] = None,
    ip_address_hint: Optional[str] = None,
) -> PrinterDevice:
    display_name = (display_name or "").strip()
    if not display_name:
        raise DeviceActionError("Отображаемое имя устройства обязательно")

    device = PrinterDevice(
        site_id=site_id,
        print_server_id=print_server_id,
        display_name=display_name,
        hostname=hostname or None,
        ip_address=ip_address or ip_address_hint or None,
        mac_address=mac_address or None,
        serial_number=serial_number or None,
        vendor=vendor or None,
        model=model or None,
        monitoring_source=MONITORING_SOURCE_DISABLED,
    )
    session.add(device)
    session.flush()
    audit.record(
        session, actor_app_user_id=actor.id, action="printer_device.create", object_type="printer_device",
        object_id=device.id, new_value={"display_name": display_name, "site_id": site_id},
    )
    return device


def set_monitoring_source(
    session: Session, *, actor: AppUser, device: PrinterDevice, source: str,
    snmp_profile_id: Optional[int] = None, zabbix_host_id: Optional[str] = None,
) -> None:
    if source not in MONITORING_SOURCES:
        raise DeviceActionError(f"Неизвестный источник мониторинга: {source!r}")
    old = {"monitoring_source": device.monitoring_source, "snmp_profile_id": device.snmp_profile_id}
    device.monitoring_source = source
    device.snmp_profile_id = snmp_profile_id
    device.zabbix_host_id = zabbix_host_id or None
    device.updated_at = utcnow()
    audit.record(
        session, actor_app_user_id=actor.id, action="printer_device.set_monitoring_source",
        object_type="printer_device", object_id=device.id, old_value=old,
        new_value={"monitoring_source": source, "snmp_profile_id": snmp_profile_id, "zabbix_host_id": zabbix_host_id},
    )


def link_queue(
    session: Session, *, actor: AppUser, device: PrinterDevice, queue: PrinterQueue,
) -> PrinterDeviceQueueLink:
    """Явная, управляемая связь — НЕ авто-сопоставление. Повторная попытка
    связать уже связанную (активную) пару — ошибка, не молчаливый no-op,
    чтобы UI не создавал впечатление нового действия там, где ничего не
    изменилось."""
    existing = (
        session.query(PrinterDeviceQueueLink)
        .filter_by(printer_device_id=device.id, printer_queue_id=queue.id)
        .first()
    )
    if existing is not None and existing.is_active:
        raise DeviceActionError("Эта очередь уже связана с этим устройством")

    now = utcnow()
    if existing is not None:
        existing.is_active = True
        existing.linked_by_id = actor.id
        existing.linked_at = now
        existing.unlinked_by_id = None
        existing.unlinked_at = None
        link = existing
    else:
        link = PrinterDeviceQueueLink(
            printer_device_id=device.id, printer_queue_id=queue.id,
            linked_by_id=actor.id, linked_at=now, is_active=True,
        )
        session.add(link)
    session.flush()
    audit.record(
        session, actor_app_user_id=actor.id, action="printer_device.link_queue", object_type="printer_device",
        object_id=device.id, new_value={"printer_queue_id": queue.id, "printer_name": queue.printer_name},
    )
    return link


def unlink_queue(session: Session, *, actor: AppUser, link: PrinterDeviceQueueLink) -> None:
    if not link.is_active:
        raise DeviceActionError("Эта связь уже неактивна")
    now = utcnow()
    link.is_active = False
    link.unlinked_by_id = actor.id
    link.unlinked_at = now
    audit.record(
        session, actor_app_user_id=actor.id, action="printer_device.unlink_queue", object_type="printer_device",
        object_id=link.printer_device_id, old_value={"printer_queue_id": link.printer_queue_id},
    )


def get_active_queue_links(session: Session, device: PrinterDevice):
    return (
        session.query(PrinterDeviceQueueLink)
        .filter_by(printer_device_id=device.id, is_active=True)
        .all()
    )
