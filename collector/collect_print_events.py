"""Инкрементальный сборщик событий печати (Event ID 307) в БД print_audit.

Запускается по расписанию (Task Scheduler, каждые 1-5 минут, см.
deploy/register_collector_task.ps1). Каждый запуск:
  1. читает курсор last_record_id из таблицы collector_state;
  2. вызывает Export-PrintEvents.ps1, который отдаёт JSON с новыми событиями;
  3. разбирает поля события по collector.field_map из config.yaml;
  4. резолвит очередь печати (создаёт как discovered/unconfigured, если её
     ещё не видела синхронизация Get-Printer), тариф и отдел (сперва по AD,
     затем легаси-CSV);
  5. пишет новые задания в print_jobs и продвигает курсор;
  6. пишет запись о самом прогоне в sync_runs (успех/ошибка, счётчики) —
     это то, что показывается в /admin (Обзор).

Идемпотентность: курсор (EventRecordID) не позволяет обработать событие дважды
при штатной работе; на случай повторного запуска/сбоя после частичной вставки
дополнительно стоит UNIQUE(print_server_id, record_id) на уровне БД (см.
printaudit.models.PrintJob — EventRecordID уникален только в пределах одного
Print Server, не площадки) и проверка существования записи перед вставкой.
Курсор продвигается и sync_runs помечается success только если весь прогон
дошёл до конца без исключения — при сбое пишется отдельная запись sync_runs
со status="failed" в отдельной транзакции (см. _record_failed_run), потому что
основная транзакция в этот момент уже откатывается.

Multisite (см. docs/MULTISITE_ARCHITECTURE.md): каждый запуск сам заводит
себе локальный Site+PrintServer (printaudit.sites.get_or_create_local_print_server),
без ручной регистрации — это то же самое поведение и в standalone-, и в
agent-режиме. В agent-режиме (APP_MODE=agent) каждое новое задание СРАЗУ
ставится в durable outbox (OutboxEvent) в ТОЙ ЖЕ транзакции — см.
scripts/agent_sync.py, который отдельно и периодически отправляет очередь в центр.
"""
import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.ad_normalize import normalize_login  # noqa: E402
from printaudit.ad_settings import get_ad_settings  # noqa: E402
from printaudit.agent_settings import is_agent_mode  # noqa: E402
from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.department_resolver import lookup_department_for_print_job_user  # noqa: E402
from printaudit.models import CollectorState, OutboxEvent, PrintJob, SyncRun  # noqa: E402
from printaudit.printers.resolver import get_or_create_printer_queue, resolve_price  # noqa: E402
from printaudit.privacy import apply_document_name_policy  # noqa: E402
from printaudit.sites import get_or_create_local_print_server  # noqa: E402

PS_SCRIPT = Path(__file__).resolve().parent / "Export-PrintEvents.ps1"


class FieldMapError(ValueError):
    """collector.field_map указывает на индекс, которого нет в Properties события —
    почти всегда означает, что калибровку (calibrate_event_fields.ps1) не проводили
    или проводили на другой версии Windows Server/драйвера. См. docs/ADMIN_GUIDE.md."""


def parse_export_output(raw: str) -> list:
    """Разбирает stdout Export-PrintEvents.ps1 в список событий (list[dict]).

    Контракт: скрипт ДОЛЖЕН отдавать JSON-массив всегда — при 0, 1 и N событиях.
    На практике встречался баг, когда `ConvertTo-Json` разворачивал переданный
    через pipe массив из ровно одного элемента и отдавал JSON-объект вместо
    массива (PowerShell разворачивает коллекции в пайплайне поэлементно).
    Этот разбор на стороне Python — вторая линия защиты: даже если PowerShell
    когда-нибудь снова отдаст «голый» объект, мы аккуратно оборачиваем его в
    список, а любой другой неожиданный формат превращаем в понятную ошибку,
    а не в TypeError при итерации.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:500]
        raise RuntimeError(
            f"Export-PrintEvents.ps1 вернул невалидный JSON: {exc}. "
            f"Начало вывода: {snippet!r}"
        ) from exc

    if isinstance(data, dict):
        # Один объект вместо массива (см. docstring выше) — нормализуем.
        data = [data]

    if not isinstance(data, list):
        raise RuntimeError(
            f"Export-PrintEvents.ps1 вернул JSON неожиданного типа "
            f"{type(data).__name__} (ожидался массив событий)."
        )

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"Export-PrintEvents.ps1: элемент #{i} в массиве событий имеет тип "
                f"{type(item).__name__}, ожидался объект события."
            )

    return data


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("collector")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / "collector.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger


def fetch_events(log_name: str, event_id: int, after_record_id: int, max_events: int) -> list:
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(PS_SCRIPT),
        "-LogName", log_name,
        "-EventId", str(event_id),
        "-AfterRecordId", str(after_record_id),
        "-MaxEvents", str(max_events),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Export-PrintEvents.ps1 завершился с ошибкой: {result.stderr.strip()}")
    return parse_export_output(result.stdout)


def get_field(properties: list, field_map: dict, name: str, default=None, required_index=False):
    """Достаёт значение поля `name` из Properties события по индексу из field_map.

    Если поле не упомянуто в field_map — считается необязательным, возвращается
    `default` без ошибки (например, job_id). Если поле упомянуто, но индекс
    выходит за пределы фактического списка Properties — это почти всегда
    признак неоткалиброванного field_map (индексы поля различаются между
    Windows Server 2016/2019/2022 и версией драйвера, см. docs/ADMIN_GUIDE.md),
    и вместо тихого None лучше явно сообщить об этом (FieldMapError), чтобы
    ошибка калибровки была видна в логе, а не превращалась в пустые/нулевые
    значения в отчётах.
    """
    idx = field_map.get(name)
    if idx is None:
        return default
    if idx < 0 or idx >= len(properties):
        raise FieldMapError(
            f"collector.field_map.{name}={idx} вне диапазона: событие содержит "
            f"только {len(properties)} свойств (индексы 0..{len(properties) - 1}). "
            f"Индексы полей отличаются между серверами/драйверами — запустите "
            f"collector/calibrate_event_fields.ps1 на этом сервере и поправьте "
            f"config.yaml (см. docs/ADMIN_GUIDE.md, раздел 3)."
        )
    return properties[idx]


def get_or_create_state(session, site_code: str) -> CollectorState:
    state = session.get(CollectorState, site_code)
    if state is None:
        state = CollectorState(site_code=site_code, last_record_id=0)
        session.add(state)
        session.commit()
    return state


def _record_failed_run(settings, started_at, error_message: str) -> None:
    """Пишет неуспешный прогон в sync_runs ОТДЕЛЬНОЙ транзакцией/сессией —
    основная сессия к этому моменту уже откатывается, и любая запись,
    добавленная в неё, будет потеряна вместе с rollback."""
    session = SessionLocal()
    try:
        session.add(
            SyncRun(
                run_type="collector",
                site_code=settings.site_code,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                status="failed",
                error_message=error_message[:2000],
            )
        )
        session.commit()
    finally:
        session.close()


def run_once() -> None:
    settings = get_settings()
    ad_settings = get_ad_settings()
    log = setup_logging(settings.log_dir)
    run_started_at = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        # Локальный Print Server ЭТОГО процесса — заводится автоматически
        # (site_code/server_name из config.yaml), без ручной регистрации.
        # Нужен и standalone-, и agent-режиму: делает print_jobs.print_server_id
        # / printer_queues всегда заполненными, что и есть настоящий ключ
        # идемпотентности теперь (см. printaudit.models.PrintJob).
        print_server = get_or_create_local_print_server(session, settings)
        outbox_enabled = is_agent_mode()

        state = get_or_create_state(session, settings.site_code)
        events = fetch_events(
            settings.log_name, settings.event_id, state.last_record_id, settings.max_events_per_run
        )
        log.info("Получено %d новых событий (курсор был %s)", len(events), state.last_record_id)

        max_record_id = state.last_record_id
        inserted, skipped, duplicates = 0, 0, 0

        for evt in events:
            record_id = evt["RecordId"]
            max_record_id = max(max_record_id, record_id)
            props = evt.get("Properties", [])
            fm = settings.field_map

            existing = (
                session.query(PrintJob)
                .filter_by(print_server_id=print_server.id, record_id=record_id)
                .first()
            )
            if existing:
                duplicates += 1
                continue

            try:
                job_id = get_field(props, fm, "job_id")
                document_name = get_field(props, fm, "document_name", "") or ""
                user_name = str(get_field(props, fm, "user_name", "") or "").strip()
                printer_name = str(get_field(props, fm, "printer_name", "") or "").strip()
                total_pages = int(get_field(props, fm, "total_pages", 0) or 0)
                # Необязательные поля — калибруются в field_map ТОЛЬКО если
                # реально найдены и надёжны на конкретном сервере/драйвере;
                # если поле не упомянуто в field_map, get_field() возвращает
                # None без ошибки, и мы не выдумываем значение (см.
                # docs/MULTISITE_ARCHITECTURE.md про total_pages/copies).
                source_computer_raw = get_field(props, fm, "source_computer", None)
                copies_raw = get_field(props, fm, "copies", None)
                pages_per_copy_raw = get_field(props, fm, "pages_per_copy", None)
                copies = int(copies_raw) if copies_raw not in (None, "") else None
                pages_per_copy = int(pages_per_copy_raw) if pages_per_copy_raw not in (None, "") else None
            except (TypeError, ValueError) as exc:
                log.warning(
                    "RecordId=%s: не удалось разобрать поля (%s). Проверьте калибровку "
                    "field_map (collector/calibrate_event_fields.ps1). Properties=%s",
                    record_id, exc, props,
                )
                skipped += 1
                continue

            if not user_name or not printer_name:
                log.warning(
                    "RecordId=%s: пустой user_name или printer_name — пропущено. "
                    "Возможно, неверные индексы field_map. Properties=%s",
                    record_id, props,
                )
                skipped += 1
                continue

            time_created = datetime.strptime(evt["TimeCreated"], "%Y-%m-%dT%H:%M:%S.%f%z")

            # Очередь печати: если её ещё не видела синхронизация Get-Printer
            # (/admin -> Принтеры -> "Обнаружить очереди"), заводим сами как
            # discovered/unconfigured — учёт не блокируется отсутствием ручной настройки.
            printer_queue = get_or_create_printer_queue(session, printer_name, print_server.id)
            if not printer_queue.collection_enabled:
                log.info(
                    "RecordId=%s: учёт для очереди '%s' отключён (collection_enabled=False) — пропущено.",
                    record_id, printer_name,
                )
                skipped += 1
                continue
            printer_queue.last_job_at = time_created

            resolution = resolve_price(session, printer_queue, time_created, settings)
            cost = round(total_pages * resolution.price_per_page, 2)

            department_id = lookup_department_for_print_job_user(
                session, user_name, ad_domain=ad_settings.domain or None
            )

            job = PrintJob(
                site_code=settings.site_code,
                site_id=print_server.site_id,
                print_server_id=print_server.id,
                record_id=record_id,
                job_id=str(job_id) if job_id is not None else None,
                time_created=time_created,
                user_name=user_name,
                user_login_normalized=normalize_login(user_name),
                document_name=apply_document_name_policy(document_name, settings.document_name_policy),
                printer_name=printer_name,
                source_computer=str(source_computer_raw).strip() if source_computer_raw else None,
                total_pages=total_pages,
                copies=copies,
                pages_per_copy=pages_per_copy,
                is_color=resolution.is_color,
                color_source=resolution.color_source,
                department_id=department_id,
                printer_queue_id=printer_queue.id,
                price_rule_id=resolution.price_rule_id,
                price_per_page=resolution.price_per_page,
                currency=resolution.currency,
                cost=cost,
            )
            session.add(job)
            if outbox_enabled:
                # Атомарно с самим заданием (та же транзакция/commit) — см.
                # требование "запись PrintJob и постановка в outbox происходят
                # атомарно" (docs/MULTISITE_ARCHITECTURE.md, часть 4).
                session.add(OutboxEvent(print_job=job))
            inserted += 1

        state.last_record_id = max_record_id
        state.last_run_at = datetime.now(timezone.utc)
        session.add(
            SyncRun(
                run_type="collector",
                site_code=settings.site_code,
                started_at=run_started_at,
                finished_at=datetime.now(timezone.utc),
                status="success",
                events_fetched=len(events),
                inserted=inserted,
                skipped=skipped,
                duplicates=duplicates,
            )
        )
        session.commit()
        log.info(
            "Готово. вставлено=%d пропущено=%d дублей=%d новый_курсор=%s",
            inserted, skipped, duplicates, max_record_id,
        )
    except Exception as exc:
        session.rollback()
        log.exception("Сбой при сборе событий печати")
        _record_failed_run(settings, run_started_at, str(exc))
        raise
    finally:
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run_once()
