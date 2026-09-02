"""Обнаружение локальных очередей печати Windows Print Server (Get-Printer) и
синхронизация в таблицу printer_queues.

Полностью только чтение со стороны Windows: PowerShell-скрипт
(printers/Export-Printers.ps1) вызывает исключительно `Get-Printer`, ничего
не создаёт/не удаляет/не меняет. Исчезновение очереди из Get-Printer НИКОГДА
не удаляет её строку в printer_queues (и тем более не трогает print_jobs) —
только снимает флаг is_active.
"""
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List

from sqlalchemy.orm import Session

from printaudit.models import PrinterQueue

PS_SCRIPT = Path(__file__).resolve().parent.parent.parent / "printers" / "Export-Printers.ps1"


class PrinterDiscoveryError(RuntimeError):
    pass


def parse_printers_output(raw: str) -> list:
    """Тот же разбор с защитой от 'один элемент -> JSON-объект', что и для
    событий печати (см. collector.collect_print_events.parse_export_output) —
    один и тот же класс бага у ConvertTo-Json, один и тот же контракт."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PrinterDiscoveryError(
            f"Export-Printers.ps1 вернул невалидный JSON: {exc}. Начало вывода: {raw[:500]!r}"
        ) from exc

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise PrinterDiscoveryError(
            f"Export-Printers.ps1 вернул JSON неожиданного типа {type(data).__name__}"
        )
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise PrinterDiscoveryError(f"Элемент #{i} в списке принтеров не является объектом")
    return data


def fetch_local_printers(timeout: int = 60) -> list:
    """Запускает Export-Printers.ps1 и возвращает список словарей (raw
    Get-Printer properties). В тестах подменяется целиком (см.
    tests/test_printer_discovery.py) — реальный PowerShell не вызывается."""
    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(PS_SCRIPT),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise PrinterDiscoveryError(f"Export-Printers.ps1 завершился с ошибкой: {result.stderr.strip()}")
    return parse_printers_output(result.stdout)


@dataclass
class DiscoverySummary:
    seen: int = 0
    created: int = 0
    updated: int = 0
    newly_missing: int = 0
    reappeared: int = 0
    created_names: List[str] = field(default_factory=list)
    newly_missing_names: List[str] = field(default_factory=list)


def sync_printer_queues(
    session: Session,
    fetch_fn: Callable[[], list] = fetch_local_printers,
) -> DiscoverySummary:
    """Синхронизирует printer_queues с текущим списком Get-Printer.

    Технические поля (server_name, share_name, driver_name, port_name,
    location, comment, is_shared, is_published, printer_status) всегда
    обновляются из последнего опроса. Поля, которыми управляет администратор
    вручную (display_name, color_mode, collection_enabled, price_per_page) —
    НИКОГДА не перезаписываются синхронизацией, ни при создании (кроме
    разумных значений по умолчанию), ни при обновлении существующей записи.
    """
    raw_printers = fetch_fn()
    now = datetime.now(timezone.utc)
    summary = DiscoverySummary()

    seen_names = set()
    for raw in raw_printers:
        name = (raw.get("Name") or "").strip()
        if not name:
            continue
        seen_names.add(name)
        summary.seen += 1

        queue = session.query(PrinterQueue).filter_by(printer_name=name).first()
        if queue is None:
            queue = PrinterQueue(
                printer_name=name,
                display_name=name,
                first_seen_at=now,
                color_mode="unknown",
                collection_enabled=True,
            )
            session.add(queue)
            summary.created += 1
            summary.created_names.append(name)
        else:
            if not queue.is_active:
                summary.reappeared += 1
            summary.updated += 1

        queue.server_name = raw.get("ComputerName") or None
        queue.share_name = raw.get("ShareName") or None
        queue.driver_name = raw.get("DriverName") or None
        queue.port_name = raw.get("PortName") or None
        queue.location = raw.get("Location") or None
        queue.comment = raw.get("Comment") or None
        queue.is_shared = bool(raw.get("Shared"))
        queue.is_published = bool(raw.get("Published"))
        queue.printer_status = raw.get("PrinterStatus") or None
        queue.is_active = True
        queue.last_seen_at = now
        queue.updated_at = now

    # SQLAlchemy обрабатывает in_(set()) корректно (получившийся ~in_() значит
    # "не совпало ничего из пустого набора", т.е. всегда True) — специальный
    # случай "ничего не обнаружено" не нужен, все активные очереди попадут в missing.
    missing = (
        session.query(PrinterQueue)
        .filter(PrinterQueue.is_active.is_(True))
        .filter(~PrinterQueue.printer_name.in_(seen_names))
        .all()
    )
    for queue in missing:
        queue.is_active = False
        queue.updated_at = now
        summary.newly_missing += 1
        summary.newly_missing_names.append(queue.printer_name)

    return summary
