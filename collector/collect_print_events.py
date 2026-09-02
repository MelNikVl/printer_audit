"""Инкрементальный сборщик событий печати (Event ID 307) в БД print_audit.

Запускается по расписанию (Task Scheduler, каждые 1-5 минут, см.
deploy/register_collector_task.ps1). Каждый запуск:
  1. читает курсор last_record_id из таблицы collector_state;
  2. вызывает Export-PrintEvents.ps1, который отдаёт JSON с новыми событиями;
  3. разбирает поля события по collector.field_map из config.yaml;
  4. подбирает отдел (по users) и тариф (по price_list);
  5. пишет новые задания в print_jobs и продвигает курсор.

Идемпотентность: курсор (EventRecordID) не позволяет обработать событие дважды
при штатной работе; на случай повторного запуска/сбоя после частичной вставки
дополнительно стоит UNIQUE(site_code, record_id) на уровне БД и проверка
существования записи перед вставкой.
"""
import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import CollectorState, PrintJob, User  # noqa: E402
from printaudit.pricing import match_price  # noqa: E402

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


def run_once() -> None:
    settings = get_settings()
    log = setup_logging(settings.log_dir)
    session = SessionLocal()
    try:
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
                .filter_by(site_code=settings.site_code, record_id=record_id)
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

            user = session.get(User, user_name)
            if user is None:
                user = User(user_name=user_name, department_id=None, is_active=True)
                session.add(user)
                session.flush()

            price_per_page, is_color, _currency = match_price(session, printer_name, settings)
            cost = round(total_pages * price_per_page, 2)

            time_created = datetime.strptime(evt["TimeCreated"], "%Y-%m-%dT%H:%M:%S.%f%z")

            job = PrintJob(
                site_code=settings.site_code,
                record_id=record_id,
                job_id=str(job_id) if job_id is not None else None,
                time_created=time_created,
                user_name=user_name,
                document_name=document_name,
                printer_name=printer_name,
                total_pages=total_pages,
                is_color=is_color,
                department_id=user.department_id,
                price_per_page=price_per_page,
                cost=cost,
            )
            session.add(job)
            inserted += 1

        state.last_record_id = max_record_id
        state.last_run_at = datetime.now(timezone.utc)
        session.commit()
        log.info(
            "Готово. вставлено=%d пропущено=%d дублей=%d новый_курсор=%s",
            inserted, skipped, duplicates, max_record_id,
        )
    except Exception:
        session.rollback()
        log.exception("Сбой при сборе событий печати")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run_once()
