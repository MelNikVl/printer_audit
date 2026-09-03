"""Отправка накопленного durable outbox (см. printaudit.models.OutboxEvent)
в центральный Print Audit — работает только в режиме APP_MODE=agent (см.
printaudit/agent_settings.py). Запускается по расписанию (Task Scheduler,
каждые 1-2 минуты, см. deploy/register_agent_sync_task.ps1) — точно так же,
как collector/collect_print_events.py, но отдельным заданием: сбор
локальных событий печати и отправка их в центр НЕ должны блокировать друг
друга (недоступность центра не должна останавливать локальный сбор).

Каждый запуск:
  1. читает из outbox все строки со status != "delivered", у которых
     next_attempt_at пуст или уже наступил (лимит — agent.max_batch_size
     из config.yaml);
  2. собирает по ним один пакет (POST /api/v1/agent/events/batch);
  3. по ответу помечает "inserted"/"duplicate" как delivered, "rejected" —
     оставляет как failed с текстом ошибки (взгляд для диагностики, но
     всё равно продолжает попадать в следующие попытки — сервер может
     начать принимать событие после ручного исправления на своей стороне,
     например создания отсутствующего отдела);
  4. при полном сбое запроса (сеть/timeout/5xx) НИЧЕГО не помечает
     delivered — увеличивает attempts и планирует следующую попытку с
     экспоненциальным backoff + jitter, не бросая исключение наружу
     (задание Task Scheduler должно завершаться штатно и не шуметь в лог
     ошибками, ожидаемыми при временной недоступности центра);
  5. отдельно шлёт heartbeat (agent_version, pending_queue_size,
     last_error) — даже если пакет событий пуст, чтобы центр видел агента
     "online", пока сеть в порядке.

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

from printaudit.agent_settings import AGENT_VERSION, PROTOCOL_VERSION, get_agent_settings, is_agent_mode  # noqa: E402
from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import OutboxEvent, PrintJob  # noqa: E402
from printaudit.timeutil import naive_utc, utcnow  # noqa: E402

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 1800
BACKOFF_JITTER_RATIO = 0.3


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
    now = naive_utc(utcnow())
    return (
        session.query(OutboxEvent)
        .filter(OutboxEvent.status != "delivered")
        .filter((OutboxEvent.next_attempt_at.is_(None)) | (OutboxEvent.next_attempt_at <= now))
        .order_by(OutboxEvent.id.asc())
        .limit(limit)
        .all()
    )


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


def run_once() -> None:
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
                for row in rows:
                    ack = by_record_id.get(row.print_job.record_id)
                    if ack is None:
                        continue
                    row.attempts += 1
                    if ack["status"] in ("inserted", "duplicate"):
                        row.status = "delivered"
                        row.delivered_at = now
                        row.last_error = None
                    else:
                        row.status = "failed"
                        row.last_error = ack.get("error") or "отклонено центральным сервером"
                        row.next_attempt_at = now + timedelta(seconds=compute_backoff_seconds(row.attempts))
                session.commit()
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

        pending_count = (
            session.query(OutboxEvent).filter(OutboxEvent.status != "delivered").count()
        )
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
                        "last_error": last_error,
                    },
                )
        except AgentSyncError as exc:
            log.warning("Heartbeat не доставлен: %s", exc)
    finally:
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run_once()
