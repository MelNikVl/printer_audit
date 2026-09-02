"""Тесты версионированных тарифов (price_rules): приоритет, период действия,
специфичность очереди vs дефолт, цепочка fallback, и что уже вставленная
стоимость задания не меняется при последующем изменении тарифа."""
from datetime import datetime, timedelta, timezone


def test_default_rule_applies_when_no_queue_specific_rule(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PriceRule
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-BW")
    session.add(PriceRule(printer_queue_id=None, is_color=False, price_per_page=5.0, priority=0))
    session.commit()

    from printaudit.config import get_settings
    price, is_color, currency, rule_id = resolve_price(session, queue, datetime.now(timezone.utc), get_settings())
    assert price == 5.0
    assert is_color is False
    session.close()


def test_queue_specific_rule_beats_default_regardless_of_priority(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PriceRule
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-Color")
    session.add(PriceRule(printer_queue_id=None, is_color=False, price_per_page=5.0, priority=100))
    session.add(PriceRule(printer_queue_id=queue.id, is_color=True, price_per_page=40.0, priority=0))
    session.commit()

    from printaudit.config import get_settings
    price, is_color, currency, rule_id = resolve_price(session, queue, datetime.now(timezone.utc), get_settings())
    assert price == 40.0
    assert is_color is True
    session.close()


def test_higher_priority_rule_wins_among_queue_specific_rules(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PriceRule
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-BW")
    session.add(PriceRule(printer_queue_id=queue.id, is_color=False, price_per_page=8.0, priority=0))
    session.add(PriceRule(printer_queue_id=queue.id, is_color=False, price_per_page=6.0, priority=5))
    session.commit()

    from printaudit.config import get_settings
    price, *_ = resolve_price(session, queue, datetime.now(timezone.utc), get_settings())
    assert price == 6.0
    session.close()


def test_expired_rule_is_not_applied(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PriceRule
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-BW")
    now = datetime.now(timezone.utc)
    session.add(
        PriceRule(
            printer_queue_id=queue.id, is_color=False, price_per_page=99.0,
            valid_from=now - timedelta(days=30), valid_to=now - timedelta(days=1),
        )
    )
    session.add(PriceRule(printer_queue_id=None, is_color=False, price_per_page=5.0))
    session.commit()

    from printaudit.config import get_settings
    price, *_ = resolve_price(session, queue, now, get_settings())
    assert price == 5.0  # старое правило истекло, применился дефолт
    session.close()


def test_future_rule_is_not_yet_applied(app_env):
    from printaudit.database import SessionLocal
    from printaudit.models import PriceRule
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-BW")
    now = datetime.now(timezone.utc)
    session.add(PriceRule(printer_queue_id=queue.id, is_color=False, price_per_page=99.0, valid_from=now + timedelta(days=1)))
    session.add(PriceRule(printer_queue_id=None, is_color=False, price_per_page=5.0))
    session.commit()

    from printaudit.config import get_settings
    price, *_ = resolve_price(session, queue, now, get_settings())
    assert price == 5.0
    session.close()


def test_fallback_to_printer_queue_quick_price_when_no_rules(app_env):
    from printaudit.database import SessionLocal
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-BW")
    queue.price_per_page = 12.5
    queue.color_mode = "color"
    session.commit()

    from printaudit.config import get_settings
    price, is_color, currency, rule_id = resolve_price(session, queue, datetime.now(timezone.utc), get_settings())
    assert price == 12.5
    assert is_color is True
    assert rule_id is None
    session.close()


def test_fallback_to_settings_default_when_nothing_configured(app_env):
    from printaudit.database import SessionLocal
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "Unconfigured-Printer")
    session.commit()

    from printaudit.config import get_settings
    settings = get_settings()
    price, is_color, currency, rule_id = resolve_price(session, queue, datetime.now(timezone.utc), settings)
    assert price == settings.default_price_bw
    assert is_color is False
    session.close()


def test_historical_cost_unchanged_after_rule_price_is_edited(app_env):
    """Требование: изменение тарифа не должно пересчитывать уже вставленные задания."""
    from printaudit.database import SessionLocal
    from printaudit.models import PriceRule, PrintJob
    from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price

    session = SessionLocal()
    queue = get_or_create_printer_queue(session, "HP-BW")
    rule = PriceRule(printer_queue_id=queue.id, is_color=False, price_per_page=10.0)
    session.add(rule)
    session.commit()

    from printaudit.config import get_settings
    price, is_color, currency, rule_id = resolve_price(session, queue, datetime.now(timezone.utc), get_settings())
    job = PrintJob(
        site_code="TEST", record_id=1, time_created=datetime.now(timezone.utc),
        user_name="DOMAIN\\ivanov", printer_name=queue.printer_name, total_pages=10,
        printer_queue_id=queue.id, price_rule_id=rule_id, price_per_page=price, cost=price * 10,
    )
    session.add(job)
    session.commit()
    job_id = job.id

    # Админ меняет цену правила задним числом.
    rule.price_per_page = 999.0
    session.commit()

    session.refresh(job)
    stored = session.get(PrintJob, job_id)
    assert stored.price_per_page == 10.0
    assert stored.cost == 100.0
    session.close()
