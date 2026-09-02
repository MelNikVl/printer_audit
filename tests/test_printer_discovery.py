"""Тесты синхронизации printer_queues с (замоканным) Get-Printer: 0/1/N
принтеров, тот же баг с одиночным JSON-объектом что и у событий печати,
появление/исчезновение очереди без удаления истории, и что синхронизация
никогда не перезаписывает поля, которыми управляет администратор вручную
(display_name, color_mode, collection_enabled, price_per_page)."""
import json

import pytest


def test_parse_zero_printers(app_env):
    from printaudit.printers.discovery import parse_printers_output

    assert parse_printers_output("[]") == []


def test_parse_single_printer_object_normalized_to_list(app_env):
    """Тот же баг ConvertTo-Json, что чинили для Export-PrintEvents.ps1."""
    from printaudit.printers.discovery import parse_printers_output

    raw = json.dumps({"Name": "HP-3F-BW"})
    result = parse_printers_output(raw)
    assert result == [{"Name": "HP-3F-BW"}]


def test_parse_malformed_json_raises(app_env):
    from printaudit.printers.discovery import PrinterDiscoveryError, parse_printers_output

    with pytest.raises(PrinterDiscoveryError):
        parse_printers_output("{not json")


def test_sync_creates_new_queue_with_defaults(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue
    from printaudit.printers.discovery import sync_printer_queues

    session = SessionLocal()
    fetch = lambda: [{"Name": "HP-3F-BW", "ShareName": "HP3F", "DriverName": "HP Universal", "Shared": True}]
    summary = sync_printer_queues(session, fetch_fn=fetch)
    session.commit()

    assert summary.created == 1
    assert summary.seen == 1
    q = session.query(PrinterQueue).filter_by(printer_name="HP-3F-BW").one()
    assert q.is_active is True
    assert q.color_mode == "unknown"
    assert q.collection_enabled is True
    assert q.is_shared is True
    session.close()


def test_sync_marks_disappeared_queue_inactive_without_deleting(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue
    from printaudit.printers.discovery import sync_printer_queues

    session = SessionLocal()
    sync_printer_queues(session, fetch_fn=lambda: [{"Name": "HP-3F-BW"}])
    session.commit()

    # Второй прогон: принтер исчез из Get-Printer.
    summary = sync_printer_queues(session, fetch_fn=lambda: [])
    session.commit()

    assert summary.newly_missing == 1
    q = session.query(PrinterQueue).filter_by(printer_name="HP-3F-BW").one()
    assert q.is_active is False  # строка осталась, история/тарифы не тронуты
    session.close()


def test_sync_preserves_admin_configured_fields_across_resync(app_env):
    """Синхронизация не должна затирать то, что вручную настроил администратор
    в админке (display_name, цвет, включённость учёта, цена)."""
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue
    from printaudit.printers.discovery import sync_printer_queues

    session = SessionLocal()
    sync_printer_queues(session, fetch_fn=lambda: [{"Name": "HP-3F-BW", "Location": "3rd floor"}])
    session.commit()

    q = session.query(PrinterQueue).filter_by(printer_name="HP-3F-BW").one()
    q.display_name = "Цветной HP на 3 этаже"
    q.color_mode = "color"
    q.collection_enabled = False
    q.price_per_page = 42.5
    session.commit()

    # Повторный прогон с изменённым Location (техническое поле, обновится),
    # но админские поля должны остаться как есть.
    sync_printer_queues(session, fetch_fn=lambda: [{"Name": "HP-3F-BW", "Location": "4th floor"}])
    session.commit()

    session.refresh(q)
    assert q.location == "4th floor"  # техническое поле обновилось
    assert q.display_name == "Цветной HP на 3 этаже"
    assert q.color_mode == "color"
    assert q.collection_enabled is False
    assert q.price_per_page == 42.5
    session.close()


def test_sync_reappeared_queue_is_reactivated(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue
    from printaudit.printers.discovery import sync_printer_queues

    session = SessionLocal()
    sync_printer_queues(session, fetch_fn=lambda: [{"Name": "HP-3F-BW"}])
    session.commit()
    sync_printer_queues(session, fetch_fn=lambda: [])  # исчез
    session.commit()

    summary = sync_printer_queues(session, fetch_fn=lambda: [{"Name": "HP-3F-BW"}])  # снова появился
    session.commit()

    assert summary.reappeared == 1
    q = session.query(PrinterQueue).filter_by(printer_name="HP-3F-BW").one()
    assert q.is_active is True
    session.close()


def test_sync_multiple_printers_at_once(app_env):
    from printaudit.database import SessionLocal
    from printaudit.printers.discovery import sync_printer_queues

    session = SessionLocal()
    summary = sync_printer_queues(
        session,
        fetch_fn=lambda: [{"Name": "A"}, {"Name": "B"}, {"Name": "C"}],
    )
    session.commit()
    assert summary.created == 3
    assert summary.seen == 3
    session.close()
