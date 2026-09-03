"""Отправка накопленного durable outbox (см. printaudit.models.OutboxEvent)
в центральный Print Audit — работает только в режиме APP_MODE=agent (см.
printaudit/agent_settings.py). Запускается по расписанию (Task Scheduler,
каждые 1-2 минуты, см. deploy/register_agent_sync_task.ps1) — точно так же,
как collector/collect_print_events.py, но отдельным заданием: сбор
локальных событий печати и отправка их в центр НЕ должны блокировать друг
друга (недоступность центра не должна останавливать локальный сбор).

Каждый запуск:
  1. читает из outbox все строки со status == "pending" (см. OutboxEvent.status
     ниже), у которых next_attempt_at пуст или уже наступил (лимит —
     agent.max_batch_size из config.yaml);
  2. собирает по ним один пакет (POST /api/v1/agent/events/batch);
  3. по ответу к каждой строке применяется ровно ОДНО из:
       - ack "inserted"/"duplicate"  -> status="delivered" (терминально, успех);
       - ack "rejected"              -> status="failed" (ТЕРМИНАЛЬНО: центр
         explicit-но отклонил событие как невалидное — это не временный
         сбой сети, автоматический повтор его не исправит, поэтому
         next_attempt_at сбрасывается в NULL и строка больше НИКОГДА не
         попадёт в следующую выборку _fetch_due_outbox_rows(). Исправление
         причины (например, донастройка чего-то на центральном сервере) и
         ПОВТОРНАЯ отправка — только вручную, см. --retry-failed ниже);
       - ack ОТСУТСТВУЕТ для этого record_id (центр вернул 200, но забыл
         этот конкретный элемент в results — protocol-нарушение с его
         стороны) -> остаётся status="pending", НЕ terminal, планируется
         retry с backoff, как и сетевой сбой;
  4. при полном сбое запроса (сеть/timeout/4xx-5xx кроме собственно
     ответа с per-event результатами) НИЧЕГО не помечает delivered/failed —
     все строки пакета остаются status="pending", увеличивается attempts и
     планируется следующая попытка с экспоненциальным backoff + jitter, не
     бросая исключение наружу (задание Task Scheduler должно завершаться
     штатно и не шуметь в лог ошибками, ожидаемыми при временной
     недоступности центра);
  5. отдельно шлёт heartbeat (agent_version, pending_queue_size —
     ТОЛЬКО retryable pending, failed_queue_size — терминально отклонённые
     отдельно, last_error) — даже если пакет событий пуст, чтобы центр
     видел агента "online", пока сеть в порядке.

Ручной повтор (`python collector\\agent_sync.py --retry-failed`) — после
того как причина отклонения устранена (например, вручную создан
отсутствующий на центре отдел), сбрасывает ВСЕ status="failed" обратно в
"pending" (next_attempt_at и attempts обнуляются) и сразу же пытается
отправить их в рамках этого же запуска — безопасная операция, идемпотентна
(повторный вызов при отсутствии failed-строк ничего не делает).

Не удаляет строки outbox после подтверждённой доставки — они остаются
delivered=True навсегда как локальная история (см. требование "не удаляй
локальную историю сразу после отправки").
"""
import argparse
import logging
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printaudit.agent_settings import (  # noqa: E402
    AGENT_VERSION,
    MONITORING_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    get_agent_settings,
    is_agent_mode,
)
from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import (  # noqa: E402
    MonitoringSyncState,
    OutboxEvent,
    PrinterAlert,
    PrinterCounterSample,
    PrinterDevice,
    PrinterHealthSample,
    PrinterSupplySample,
    PrintJob,
)
from printaudit.monitoring import MONITORING_SOURCE_SNMP, MONITORING_SOURCE_ZABBIX  # noqa: E402
from printaudit.sites import get_or_create_local_print_server  # noqa: E402
from printaudit.timeutil import naive_utc, utcnow  # noqa: E402

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 1800
BACKOFF_JITTER_RATIO = 0.3

# Сколько сэмплов/алертов максимум за один пакет мониторинга -- центр
# независимо ограничивает тем же числом со своей стороны (см.
# webapp/agent_api.py::MAX_MONITORING_ITEMS_PER_BATCH), это лимит именно
# того, сколько агент готов набрать за один запрос.
MAX_MONITORING_ITEMS_PER_BATCH = 500


class AgentSyncError(RuntimeError):
    """Сбой отправки пакета (сеть/timeout/HTTP-ошибка) — НЕ поднимается
    наружу из run_once(), только логируется, чтобы Task Scheduler не считал
    временную недоступность центра сбоем задания."""


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("agent_sync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / "agent_sync.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger


def compute_backoff_seconds(attempts: int) -> float:
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))
    jitter = base * BACKOFF_JITTER_RATIO * random.random()
    return base + jitter


def _fetch_due_outbox_rows(session, limit: int) -> List[OutboxEvent]:
    """Выбирает ТОЛЬКО retryable события — status="pending" (не "delivered"
    и не терминальный "failed"), у которых пришло время следующей попытки.
    До исправления (эта ветка pre-merge hardening) здесь ошибочно был
    фильтр `status != "delivered"`, который включал и терминально
    отклонённые "failed" строки — то есть они продолжали бесконечно
    отправляться повторно, хотя по смыслу "failed" должен быть терминальным."""
    now = naive_utc(utcnow())
    return (
        session.query(OutboxEvent)
        .filter(OutboxEvent.status == "pending")
        .filter((OutboxEvent.next_attempt_at.is_(None)) | (OutboxEvent.next_attempt_at <= now))
        .order_by(OutboxEvent.id.asc())
        .limit(limit)
        .all()
    )


def retry_failed_rows(session) -> int:
    """Ручной, явный сброс терминально отклонённых событий обратно в
    "pending" — единственный предусмотренный способ повторно отправить
    "failed" строку (автоматика их больше не трогает, см. модульный
    docstring). Возвращает число сброшенных строк. Идемпотентно: без
    failed-строк — no-op."""
    rows = session.query(OutboxEvent).filter(OutboxEvent.status == "failed").all()
    for row in rows:
        row.status = "pending"
        row.attempts = 0
        row.next_attempt_at = None
        row.last_error = None
    session.commit()
    return len(rows)


def _job_to_event_payload(job: PrintJob) -> dict:
    return {
        "record_id": job.record_id,
        "job_id": job.job_id,
        "time_created": job.time_created.replace(tzinfo=timezone.utc).isoformat(),
        "user_name": job.user_name,
        "user_login_normalized": job.user_login_normalized,
        "document_name": job.document_name,
        "printer_name": job.printer_name,
        "source_computer": job.source_computer,
        "total_pages": job.total_pages,
        "copies": job.copies,
        "pages_per_copy": job.pages_per_copy,
        "is_color": job.is_color,
        "color_source": job.color_source,
        "department_name": job.department.name if job.department else None,
        "price_per_page": job.price_per_page,
        "currency": job.currency,
        "cost": job.cost,
    }


def send_batch(client, base_url: str, token: str, timeout: float, payload: dict) -> dict:
    import httpx

    try:
        resp = client.post(
            f"{base_url.rstrip('/')}/api/v1/agent/events/batch",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        # Никогда не включаем токен в текст ошибки — httpx может отразить
        # URL/заголовки запроса в exc, но не Authorization со значением.
        raise AgentSyncError(f"Не удалось отправить пакет в центр: {exc}") from exc


def send_heartbeat(client, base_url: str, token: str, timeout: float, payload: dict) -> Optional[dict]:
    import httpx

    try:
        resp = client.post(
            f"{base_url.rstrip('/')}/api/v1/agent/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise AgentSyncError(f"Не удалось отправить heartbeat: {exc}") from exc


def send_monitoring_batch(client, base_url: str, token: str, timeout: float, payload: dict) -> dict:
    import httpx

    try:
        resp = client.post(
            f"{base_url.rstrip('/')}/api/v1/agent/monitoring/batch",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise AgentSyncError(f"Не удалось отправить мониторинговые данные в центр: {exc}") from exc


def _get_or_create_monitoring_sync_state(session, site_id: int) -> MonitoringSyncState:
    state = session.get(MonitoringSyncState, site_id)
    if state is None:
        state = MonitoringSyncState(site_id=site_id)
        session.add(state)
        session.flush()
    return state


def _build_monitoring_payload(session, site, state: MonitoringSyncState, limit: int):
    """Курсор по id для health/counter/supply (только INSERT — id
    монотонно растёт); по updated_at для алертов (они ещё и обновляются
    при resolve). Устройства отправляются ВСЕ активные с включённым
    мониторингом каждый раз, когда есть хоть один сэмпл/алерт для отправки
    — дёшево (обычно десятки-сотни устройств на площадку) и гарантирует,
    что центр успеет узнать о новом устройстве до первого сэмпла на него."""
    devices = (
        session.query(PrinterDevice)
        .filter(
            PrinterDevice.site_id == site.id,
            PrinterDevice.is_active.is_(True),
            PrinterDevice.monitoring_source.in_([MONITORING_SOURCE_ZABBIX, MONITORING_SOURCE_SNMP]),
        )
        .all()
    )
    device_by_id = {d.id: d for d in devices}

    health_rows = (
        session.query(PrinterHealthSample)
        .join(PrinterDevice, PrinterHealthSample.printer_device_id == PrinterDevice.id)
        .filter(PrinterDevice.site_id == site.id, PrinterHealthSample.id > state.last_health_sample_id)
        .order_by(PrinterHealthSample.id)
        .limit(limit)
        .all()
    )
    counter_rows = (
        session.query(PrinterCounterSample)
        .join(PrinterDevice, PrinterCounterSample.printer_device_id == PrinterDevice.id)
        .filter(PrinterDevice.site_id == site.id, PrinterCounterSample.id > state.last_counter_sample_id)
        .order_by(PrinterCounterSample.id)
        .limit(limit)
        .all()
    )
    supply_rows = (
        session.query(PrinterSupplySample)
        .join(PrinterDevice, PrinterSupplySample.printer_device_id == PrinterDevice.id)
        .filter(PrinterDevice.site_id == site.id, PrinterSupplySample.id > state.last_supply_sample_id)
        .order_by(PrinterSupplySample.id)
        .limit(limit)
        .all()
    )
    alert_cursor = naive_utc(state.last_alert_synced_at) or datetime(1970, 1, 1)
    alert_rows = (
        session.query(PrinterAlert)
        .join(PrinterDevice, PrinterAlert.printer_device_id == PrinterDevice.id)
        .filter(PrinterDevice.site_id == site.id, PrinterAlert.updated_at > alert_cursor)
        .order_by(PrinterAlert.updated_at)
        .limit(limit)
        .all()
    )

    if not (health_rows or counter_rows or supply_rows or alert_rows):
        return None

    def _device_uuid(row):
        return device_by_id.get(row.printer_device_id).uuid if row.printer_device_id in device_by_id else row.printer_device.uuid

    payload = {
        "devices": [
            {
                "device_uuid": d.uuid, "display_name": d.display_name, "hostname": d.hostname,
                "ip_address": d.ip_address, "vendor": d.vendor, "model": d.model,
            }
            for d in devices
        ],
        "health_samples": [
            {
                "device_uuid": _device_uuid(r), "collected_at": r.collected_at.replace(tzinfo=timezone.utc).isoformat(),
                "source": r.source, "is_reachable": r.is_reachable, "device_status": r.device_status,
                "has_paper_jam": r.has_paper_jam, "has_cover_open": r.has_cover_open,
                "has_paper_out": r.has_paper_out, "has_hardware_error": r.has_hardware_error,
                "raw_status_text": r.raw_status_text,
            }
            for r in health_rows
        ],
        "counter_samples": [
            {
                "device_uuid": _device_uuid(r), "collected_at": r.collected_at.replace(tzinfo=timezone.utc).isoformat(),
                "source": r.source, "total_pages": r.total_pages, "color_pages": r.color_pages, "bw_pages": r.bw_pages,
            }
            for r in counter_rows
        ],
        "supply_samples": [
            {
                "device_uuid": _device_uuid(r), "collected_at": r.collected_at.replace(tzinfo=timezone.utc).isoformat(),
                "source": r.source, "supply_type": r.supply_type, "level_percent": r.level_percent,
                "level_status": r.level_status,
            }
            for r in supply_rows
        ],
        "alerts": [
            {
                "device_uuid": _device_uuid(r), "source": r.source, "alert_type": r.alert_type,
                "severity": r.severity, "message": r.message, "external_id": r.external_id,
                "opened_at": r.opened_at.replace(tzinfo=timezone.utc).isoformat(),
                "resolved_at": r.resolved_at.replace(tzinfo=timezone.utc).isoformat() if r.resolved_at else None,
            }
            for r in alert_rows
        ],
    }
    cursors = {
        "health_id": health_rows[-1].id if health_rows else state.last_health_sample_id,
        "counter_id": counter_rows[-1].id if counter_rows else state.last_counter_sample_id,
        "supply_id": supply_rows[-1].id if supply_rows else state.last_supply_sample_id,
        "alert_at": alert_rows[-1].updated_at if alert_rows else state.last_alert_synced_at,
    }
    return payload, cursors


def sync_monitoring_data(session, agent_settings, settings, log, client_override=None) -> None:
    """Отправляет накопленные мониторинговые сэмплы/алерты этой площадки в
    центр — курсором (не полноценным outbox с pending/failed): каждое
    значение и так идемпотентно по своим UNIQUE-ограничениям на приёме
    (см. webapp/agent_api.py), поэтому повторная отправка с неподвинутым
    курсором после сбоя безопасна сама по себе, без отдельного состояния
    на строку."""
    local_server = get_or_create_local_print_server(session, settings)
    site = local_server.site
    state = _get_or_create_monitoring_sync_state(session, site.id)

    built = _build_monitoring_payload(session, site, state, MAX_MONITORING_ITEMS_PER_BATCH)
    if built is None:
        return
    body, cursors = built

    payload = {
        "protocol_version": MONITORING_PROTOCOL_VERSION,
        "site_uuid": agent_settings.site_uuid,
        "print_server_uuid": agent_settings.print_server_uuid,
        "generated_at": utcnow().isoformat(),
        **body,
    }

    state.last_attempt_at = naive_utc(utcnow())
    try:
        import httpx

        if client_override is not None:
            result = send_monitoring_batch(
                client_override, agent_settings.central_base_url, agent_settings.token,
                settings.agent_http_timeout_seconds, payload,
            )
        else:
            with httpx.Client() as client:
                result = send_monitoring_batch(
                    client, agent_settings.central_base_url, agent_settings.token,
                    settings.agent_http_timeout_seconds, payload,
                )
    except AgentSyncError as exc:
        state.last_error = str(exc)
        session.commit()
        log.warning("Мониторинговые данные не отправлены (будет повторная попытка позже): %s", exc)
        return

    state.last_health_sample_id = cursors["health_id"]
    state.last_counter_sample_id = cursors["counter_id"]
    state.last_supply_sample_id = cursors["supply_id"]
    state.last_alert_synced_at = cursors["alert_at"]
    state.last_success_at = naive_utc(utcnow())
    state.last_error = None
    session.commit()
    log.info(
        "Мониторинг отправлен: устройств=%d health=%d/%d counter=%d/%d supply=%d/%d alerts=%d",
        result.get("devices_upserted", 0),
        result.get("health_accepted", 0), result.get("health_duplicates", 0),
        result.get("counter_accepted", 0), result.get("counter_duplicates", 0),
        result.get("supply_accepted", 0), result.get("supply_duplicates", 0),
        result.get("alerts_accepted", 0),
    )


def run_once(retry_failed: bool = False) -> None:
    settings = get_settings()
    agent_settings = get_agent_settings()
    log = setup_logging(settings.log_dir)

    if not is_agent_mode():
        log.info("APP_MODE != agent — синхронизация с центром не запускается.")
        return
    if not agent_settings.is_configured:
        log.error(
            "APP_MODE=agent, но не заданы все из CENTRAL_BASE_URL/AGENT_SITE_UUID/"
            "AGENT_PRINT_SERVER_UUID/AGENT_TOKEN в .env — синхронизация невозможна."
        )
        return

    import httpx

    session = SessionLocal()
    last_error: Optional[str] = None
    try:
        if retry_failed:
            reset_count = retry_failed_rows(session)
            log.info("--retry-failed: сброшено в pending %d ранее terminal-failed событий.", reset_count)

        rows = _fetch_due_outbox_rows(session, settings.agent_max_batch_size)
        if rows:
            events = [_job_to_event_payload(row.print_job) for row in rows]
            payload = {
                "protocol_version": PROTOCOL_VERSION,
                "site_uuid": agent_settings.site_uuid,
                "print_server_uuid": agent_settings.print_server_uuid,
                "generated_at": utcnow().isoformat(),
                "events": events,
            }
            try:
                with httpx.Client() as client:
                    result = send_batch(
                        client, agent_settings.central_base_url, agent_settings.token,
                        settings.agent_http_timeout_seconds, payload,
                    )
                by_record_id = {r["record_id"]: r for r in result.get("results", [])}
                now = naive_utc(utcnow())
                missing_ack = 0
                for row in rows:
                    ack = by_record_id.get(row.print_job.record_id)
                    row.attempts += 1
                    if ack is None:
                        # Центр вернул успешный ответ, но забыл этот
                        # record_id в results — не наша вина и не признак
                        # невалидности события: остаётся pending, обычный
                        # retry с backoff, как при сетевом сбое.
                        missing_ack += 1
                        row.last_error = "Центр не вернул подтверждение для этого события в пакете"
                        row.next_attempt_at = now + timedelta(seconds=compute_backoff_seconds(row.attempts))
                    elif ack["status"] in ("inserted", "duplicate"):
                        row.status = "delivered"
                        row.delivered_at = now
                        row.last_error = None
                        row.next_attempt_at = None
                    else:
                        # ack["status"] == "rejected" -- ТЕРМИНАЛЬНО: центр
                        # явно сказал "это невалидные данные", повторная
                        # автоматическая отправка их не исправит (см.
                        # docstring модуля и retry_failed_rows() для
                        # осознанного ручного повтора после исправления причины).
                        row.status = "failed"
                        row.last_error = ack.get("error") or "отклонено центральным сервером"
                        row.next_attempt_at = None
                session.commit()
                if missing_ack:
                    log.warning("Центр не подтвердил %d событий из пакета — будет повтор.", missing_ack)
                log.info(
                    "Отправлено %d событий: принято=%d дублей=%d отклонено=%d",
                    len(events), result.get("accepted", 0), result.get("duplicates", 0), result.get("rejected", 0),
                )
            except AgentSyncError as exc:
                last_error = str(exc)
                now = naive_utc(utcnow())
                for row in rows:
                    row.attempts += 1
                    row.last_error = last_error
                    row.next_attempt_at = now + timedelta(seconds=compute_backoff_seconds(row.attempts))
                session.commit()
                log.warning("Пакет не доставлен (будет повторная попытка позже): %s", exc)
        else:
            log.info("Очередь на отправку пуста.")

        # Мониторинг принтеров -- отдельный протокол/курсор, не влияет на
        # outbox заданий печати выше и не блокируется им (см.
        # sync_monitoring_data). Сбой здесь тоже не должен ронять run_once().
        try:
            sync_monitoring_data(session, agent_settings, settings, log)
        except Exception as exc:  # noqa: BLE001 - мониторинг не должен ронять весь запуск
            log.warning("Синхронизация мониторинга принтеров не удалась: %s", exc)

        # pending_queue_size — ТОЛЬКО retryable (status="pending"); терминально
        # отклонённые считаются отдельно (failed_queue_size) и НЕ входят
        # сюда — иначе "сколько ещё реально досылать" вводило бы в
        # заблуждение (failed никогда сам не досошлётся автоматически).
        pending_count = session.query(OutboxEvent).filter(OutboxEvent.status == "pending").count()
        failed_count = session.query(OutboxEvent).filter(OutboxEvent.status == "failed").count()
        try:
            with httpx.Client() as client:
                send_heartbeat(
                    client, agent_settings.central_base_url, agent_settings.token,
                    settings.agent_http_timeout_seconds,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "site_uuid": agent_settings.site_uuid,
                        "print_server_uuid": agent_settings.print_server_uuid,
                        "agent_version": AGENT_VERSION,
                        "pending_queue_size": pending_count,
                        "failed_queue_size": failed_count,
                        "last_error": last_error,
                    },
                )
        except AgentSyncError as exc:
            log.warning("Heartbeat не доставлен: %s", exc)
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Сбросить все терминально отклонённые (status=failed) события outbox обратно в pending "
            "и сразу попытаться отправить их. Использовать вручную ПОСЛЕ того, как причина отклонения "
            "устранена (например, на центральном сервере донастроено то, чего не хватало)."
        ),
    )
    args = parser.parse_args()
    run_once(retry_failed=args.retry_failed)
