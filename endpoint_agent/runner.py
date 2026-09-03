"""Один цикл работы endpoint-агента: захват новых событий печати этого ПК
-> классификация по порту (USB/WSD/прямой IP оставляем, сетевую очередь
Print Server исключаем) -> постановка в локальную durable очередь ->
отправка накопившейся очереди на сервер площадки -> heartbeat.

Вызывается из endpoint_agent.service (Windows Service) и
endpoint_agent.main (консольный запуск/отладка) по таймеру
`poll_interval_seconds`. Сбой сети на любом шаге не должен ронять процесс —
следующий цикл повторит попытку (durable outbox переживает перезапуск)."""
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from endpoint_agent import outbox
from endpoint_agent.capture import CaptureError, fetch_new_events, fetch_port_map, parse_raw_event
from endpoint_agent.config import EndpointAgentConfig
from endpoint_agent.ports import should_capture
from endpoint_agent.sync_client import HttpResult, SyncError, send_events_batch, send_heartbeat

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 1800
BACKOFF_JITTER_RATIO = 0.3


def compute_backoff_seconds(attempts: int) -> float:
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))
    return base + base * BACKOFF_JITTER_RATIO * random.random()


def _event_to_payload(parsed) -> dict:
    return {
        "record_id": parsed.record_id,
        "job_id": parsed.job_id,
        "time_created": parsed.time_created.astimezone(timezone.utc).isoformat(),
        "user_name": parsed.user_name,
        "document_name": parsed.document_name,
        "printer_name": parsed.printer_name,
        "total_pages": parsed.total_pages,
    }


def capture_cycle(cfg: EndpointAgentConfig, conn, log: logging.Logger, event_runner=None, port_runner=None) -> int:
    """Возвращает число событий, добавленных в локальную очередь."""
    after = outbox.get_cursor(conn)
    try:
        raw_events = fetch_new_events(cfg, after, runner=event_runner)
    except CaptureError as exc:
        log.error("Не удалось прочитать журнал печати: %s", exc)
        return 0
    if not raw_events:
        return 0

    try:
        port_map = fetch_port_map(runner=port_runner)
    except CaptureError as exc:
        log.error("Не удалось получить снимок портов принтеров (Get-Printer): %s — цикл пропущен.", exc)
        return 0

    max_record_id = after
    to_enqueue = []
    for evt in raw_events:
        record_id = evt.get("RecordId")
        if isinstance(record_id, int):
            max_record_id = max(max_record_id, record_id)
        try:
            parsed = parse_raw_event(evt, cfg.field_map)
        except CaptureError as exc:
            log.warning("Событие record_id=%s пропущено: %s", record_id, exc)
            continue

        keep, reason = should_capture(parsed.printer_name, port_map, cfg.printer_allowlist, cfg.printer_denylist)
        if not keep:
            log.debug("record_id=%s принтер=%s пропущен (%s)", parsed.record_id, parsed.printer_name, reason)
            continue

        to_enqueue.append(_event_to_payload(parsed))

    inserted = outbox.enqueue_events(conn, to_enqueue) if to_enqueue else 0
    outbox.set_cursor(conn, max_record_id)
    log.info(
        "Захват: получено=%d поставлено_в_очередь=%d новый_курсор=%d", len(raw_events), inserted, max_record_id,
    )
    return inserted


def sync_cycle(cfg: EndpointAgentConfig, conn, log: logging.Logger, transport=None) -> Optional[str]:
    """Отправляет накопившуюся очередь и heartbeat. Возвращает текст
    последней ошибки (для heartbeat.last_error) или None при полном успехе."""
    last_error: Optional[str] = None
    rows = outbox.fetch_due_batch(conn, cfg.batch_size)
    if rows:
        payload = {
            "protocol_version": 1,
            "endpoint_uuid": cfg.endpoint_uuid,
            "hostname": cfg.hostname,
            "agent_version": cfg.agent_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "events": [row.payload for row in rows],
        }
        try:
            result = send_events_batch(cfg.server_base_url, cfg.token, payload, cfg.http_timeout_seconds, transport)
        except SyncError as exc:
            last_error = str(exc)
            log.warning("Пакет не отправлен (будет повтор): %s", exc)
            now = datetime.now(timezone.utc)
            for row in rows:
                outbox.mark_retry(conn, row.id, last_error, now + timedelta(seconds=compute_backoff_seconds(row.attempts + 1)))
        else:
            last_error = _apply_batch_result(conn, rows, result, log)

    heartbeat_payload = {
        "protocol_version": 1,
        "endpoint_uuid": cfg.endpoint_uuid,
        "hostname": cfg.hostname,
        "agent_version": cfg.agent_version,
        "pending_queue_size": outbox.pending_count(conn),
        "failed_queue_size": outbox.failed_count(conn),
        "last_error": last_error,
    }
    try:
        send_heartbeat(cfg.server_base_url, cfg.token, heartbeat_payload, cfg.http_timeout_seconds, transport)
    except SyncError as exc:
        log.warning("Heartbeat не отправлен: %s", exc)

    return last_error


def _apply_batch_result(conn, rows, result: HttpResult, log: logging.Logger) -> Optional[str]:
    if result.status_code != 200:
        # Ошибка на уровне ВСЕГО пакета (протокол/токен/лимиты) -- не то же
        # самое, что поэлементный "rejected" в results (см.
        # webapp/endpoint_api.py): такие ошибки затрагивают весь пакет
        # одинаково, поэтому весь пакет остаётся pending с общим backoff,
        # а не терминально проваливается -- после того как администратор
        # поправит причину (например, обновит токен), пакет уйдёт сам.
        error = f"HTTP {result.status_code}: {result.body}"
        log.warning("Пакет отклонён целиком: %s", error)
        now = datetime.now(timezone.utc)
        for row in rows:
            outbox.mark_retry(conn, row.id, error, now + timedelta(seconds=compute_backoff_seconds(row.attempts + 1)))
        return error

    by_record_id = {r["record_id"]: r for r in result.body.get("results", [])}
    now = datetime.now(timezone.utc)
    last_error = None
    for row in rows:
        ack = by_record_id.get(row.record_id)
        if ack is None:
            last_error = "Сервер не подтвердил это событие в ответе"
            outbox.mark_retry(conn, row.id, last_error, now + timedelta(seconds=compute_backoff_seconds(row.attempts + 1)))
        elif ack["status"] in ("inserted", "duplicate"):
            outbox.mark_sent(conn, [row.id])
        else:
            last_error = ack.get("error") or "отклонено сервером"
            outbox.mark_failed_terminal(conn, row.id, last_error)

    accepted = result.body.get("accepted", 0)
    duplicates = result.body.get("duplicates", 0)
    rejected = result.body.get("rejected", 0)
    log.info("Отправлено %d событий: принято=%d дублей=%d отклонено=%d", len(rows), accepted, duplicates, rejected)
    return last_error


def run_cycle(cfg: EndpointAgentConfig, conn, log: logging.Logger, event_runner=None, port_runner=None, transport=None) -> None:
    capture_cycle(cfg, conn, log, event_runner=event_runner, port_runner=port_runner)
    sync_cycle(cfg, conn, log, transport=transport)
