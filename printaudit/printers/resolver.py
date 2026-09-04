"""Резолвинг PrinterQueue и применимого тарифа для задания печати.

Порядок поиска тарифа (первое совпадение побеждает):
  1. Активный PriceRule, привязанный именно к этой очереди (printer_queue_id),
     действующий на момент задания (valid_from/valid_to), с наибольшим priority.
  2. Активный PriceRule "по умолчанию" (printer_queue_id=NULL) на тех же условиях.
  3. Быстрый тариф очереди (PrinterQueue.price_per_page), если администратор
     задал его напрямую без создания полноценного PriceRule.
  4. Легаси price_list (см. printaudit.pricing.match_price) — для обратной
     совместимости с тарифами, заведёнными до этой ветки.
  5. Цена по умолчанию из config.yaml (settings.default_price_bw).

Найденная на момент вставки цена (price_per_page, cost) и id сработавшего
правила (price_rule_id) сохраняются в print_jobs НЕОБРАТИМО — последующее
изменение/удаление тарифа не пересчитывает уже вставленные задания.

Цветность (is_color) — ВСЕГДА tri-state (True/False/None), НЕ bool с тихим
дефолтом в False. `color_source` объясняет, откуда взято значение:
  - "queue"   — из явной настройки очереди/правила/price_list (администратор
                сам сказал "эта очередь цветная/чёрно-белая");
  - "event"   — из самого события печати (Event 307 в этом проекте такого
                свойства НЕ предоставляет, см. docs/RESEARCH.md и
                docs/MULTISITE_ARCHITECTURE.md — зарезервировано на будущее
                и для central API, куда агент теоретически может передать
                более точные данные);
  - "unknown" — цвет достоверно не определён; is_color=None. Цена для
                биллинга в этом случае консервативно берётся по Ч/Б тарифу
                (settings.default_price_bw), но это НЕ то же самое, что
                утверждение "печать была чёрно-белой" — отчёты должны
                показывать "не определено", а не "Ч/б" (см. webapp/main.py).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from printaudit.models import PriceRule, PrinterQueue
from printaudit.pricing import match_price

COLOR_SOURCE_EVENT = "event"
COLOR_SOURCE_QUEUE = "queue"
COLOR_SOURCE_UNKNOWN = "unknown"


@dataclass
class PriceResolution:
    price_per_page: float
    is_color: Optional[bool]
    currency: str
    price_rule_id: Optional[int]
    color_source: str = COLOR_SOURCE_UNKNOWN


def get_or_create_printer_queue(
    session: Session, printer_name: str, print_server_id: Optional[int] = None,
    endpoint_agent_id: Optional[int] = None,
) -> PrinterQueue:
    """Если задание печатается через очередь, которую ещё не видел ни
    'Обнаружить очереди' (Get-Printer), ни этот резолвер — коллектор создаёт
    её сам как discovered_by_collector=True, unconfigured (color_mode=unknown,
    collection_enabled=True по умолчанию), не блокируя учёт.

    Очередь ищется/создаётся В ПРЕДЕЛАХ ОДНОГО источника — print_server_id
    (см. uq_printer_queues_server_name) ИЛИ endpoint_agent_id (см.
    uq_printer_queues_endpoint_name на PrinterQueue), ровно один из двух —
    одноимённые очереди/локальные принтеры на разных серверах/площадках/ПК
    больше не считаются одной и той же записью (см.
    docs/PRINTER_MONITORING_FORECASTING.md)."""
    printer_name = (printer_name or "").strip()
    now = datetime.now(timezone.utc)
    queue = (
        session.query(PrinterQueue)
        .filter_by(printer_name=printer_name, print_server_id=print_server_id, endpoint_agent_id=endpoint_agent_id)
        .first()
    )
    if queue is None:
        queue = PrinterQueue(
            printer_name=printer_name,
            print_server_id=print_server_id,
            endpoint_agent_id=endpoint_agent_id,
            display_name=printer_name,
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
            color_mode="unknown",
            collection_enabled=True,
            discovered_by_collector=True,
        )
        session.add(queue)
        session.flush()
    else:
        queue.last_seen_at = now
    return queue


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite не хранит offset у DateTime-колонок — значения, записанные как
    aware (datetime.now(timezone.utc)), читаются обратно naive. Приводим обе
    стороны сравнения к naive UTC, чтобы не зависеть от того, откуда пришло
    значение (только что созданный объект vs то, что вернула БД)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _rule_applies_at(rule: PriceRule, at: datetime) -> bool:
    at = _naive_utc(at)
    valid_from = _naive_utc(rule.valid_from)
    valid_to = _naive_utc(rule.valid_to)
    if valid_from and at < valid_from:
        return False
    if valid_to and at >= valid_to:
        return False
    return True


def resolve_price_rule(session: Session, printer_queue: PrinterQueue, at: datetime) -> Optional[PriceRule]:
    candidates = (
        session.query(PriceRule)
        .filter(
            PriceRule.is_active.is_(True),
            or_(PriceRule.printer_queue_id == printer_queue.id, PriceRule.printer_queue_id.is_(None)),
        )
        .all()
    )
    applicable = [r for r in candidates if _rule_applies_at(r, at)]
    if not applicable:
        return None
    # Правило конкретной очереди всегда важнее общего дефолта, независимо от
    # priority; внутри одной "специфичности" решает priority, затем меньший id.
    applicable.sort(
        key=lambda r: (r.printer_queue_id is None, -r.priority, r.id)
    )
    return applicable[0]


def resolve_price(
    session: Session, printer_queue: PrinterQueue, at: datetime, settings
) -> PriceResolution:
    rule = resolve_price_rule(session, printer_queue, at)
    if rule is not None:
        # Правило (queue-specific или дефолтное) — явная настройка
        # администратора, is_color всегда определённый bool.
        return PriceResolution(
            price_per_page=rule.price_per_page, is_color=rule.is_color, currency=rule.currency,
            price_rule_id=rule.id, color_source=COLOR_SOURCE_QUEUE,
        )

    if printer_queue.price_per_page is not None:
        # color_mode остаётся tri-state здесь: "unknown" на очереди должно
        # остаться is_color=None, а НЕ тихо стать False (это и был баг,
        # который эта ветка исправляет — раньше сравнение "color_mode ==
        # 'color'" делало 'unknown' и 'bw' неразличимыми).
        if printer_queue.color_mode == "color":
            is_color, color_source = True, COLOR_SOURCE_QUEUE
        elif printer_queue.color_mode == "bw":
            is_color, color_source = False, COLOR_SOURCE_QUEUE
        else:
            is_color, color_source = None, COLOR_SOURCE_UNKNOWN
        return PriceResolution(
            price_per_page=printer_queue.price_per_page, is_color=is_color,
            currency=printer_queue.currency, price_rule_id=None, color_source=color_source,
        )

    price, is_color, currency, color_source = match_price(session, printer_queue.printer_name, settings)
    return PriceResolution(
        price_per_page=price, is_color=is_color, currency=currency,
        price_rule_id=None, color_source=color_source,
    )
