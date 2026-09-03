"""Тесты durable outbox агента (collector/agent_sync.py) и его атомарной
постановки в collector/collect_print_events.py: событие ставится в очередь
в той же транзакции, что и само задание; повторная/частично повторяющаяся
отправка не создаёт дублей; недоступность центра не помечает события
доставленными и планирует повтор с backoff; после восстановления связи
очередь досылается автоматически; очередь переживает "перезапуск" (новая
сессия к той же БД)."""
import json

import pytest


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, module, stdout, returncode=0, stderr=""):
    def _fake_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return _FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(module.subprocess, "run", _fake_run)


def _one_event(record_id=1, printer="HP-3F-BW", user="DOMAIN\\ivanov", pages=5):
    return {
        "RecordId": record_id,
        "TimeCreated": "2026-09-02T10:15:00.000Z",
        "Message": "job",
        "Properties": ["1001", "report.docx", user, "x", printer, "y", "z", "w", pages],
    }


def _enable_agent_mode(monkeypatch):
    monkeypatch.setenv("APP_MODE", "agent")
    monkeypatch.setenv("CENTRAL_BASE_URL", "https://central.example.local")
    monkeypatch.setenv("AGENT_SITE_UUID", "site-uuid-1")
    monkeypatch.setenv("AGENT_PRINT_SERVER_UUID", "server-uuid-1")
    monkeypatch.setenv("AGENT_TOKEN", "test-agent-token")


def test_outbox_not_created_in_standalone_mode(app_env, monkeypatch):
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event()))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        assert session.query(OutboxEvent).count() == 0
    finally:
        session.close()


def test_outbox_enqueued_atomically_with_print_job_in_agent_mode(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event(record_id=501)))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent, PrintJob

    session = SessionLocal()
    try:
        job = session.query(PrintJob).filter_by(record_id=501).one()
        outbox = session.query(OutboxEvent).filter_by(print_job_id=job.id).one()
        assert outbox.status == "pending"
        assert outbox.attempts == 0
    finally:
        session.close()


def test_agent_sync_sends_pending_events_and_marks_delivered(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event(record_id=1)))
    cpe.run_once()

    def _fake_send_batch(client, base_url, token, timeout, payload):
        assert token == "test-agent-token"
        return {
            "accepted": len(payload["events"]), "duplicates": 0, "rejected": 0,
            "results": [{"record_id": e["record_id"], "status": "inserted"} for e in payload["events"]],
        }

    def _fake_send_heartbeat(client, base_url, token, timeout, payload):
        return {"ok": True, "server_status": "online"}

    monkeypatch.setattr(agent_sync, "send_batch", _fake_send_batch)
    monkeypatch.setattr(agent_sync, "send_heartbeat", _fake_send_heartbeat)
    agent_sync.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        outbox = session.query(OutboxEvent).one()
        assert outbox.status == "delivered"
        assert outbox.delivered_at is not None
    finally:
        session.close()


def test_agent_sync_duplicate_ack_also_marks_delivered(app_env, monkeypatch):
    """Центр отвечает "duplicate" (например, событие уже было принято ранее,
    но подтверждение потерялось из-за сетевого сбоя) — локально это тоже
    успех, не ошибка."""
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event(record_id=7)))
    cpe.run_once()

    def _fake_send_batch(client, base_url, token, timeout, payload):
        return {
            "accepted": 0, "duplicates": 1, "rejected": 0,
            "results": [{"record_id": 7, "status": "duplicate"}],
        }

    monkeypatch.setattr(agent_sync, "send_batch", _fake_send_batch)
    monkeypatch.setattr(agent_sync, "send_heartbeat", lambda *a, **k: {"ok": True, "server_status": "online"})
    agent_sync.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        assert session.query(OutboxEvent).one().status == "delivered"
    finally:
        session.close()


def test_agent_sync_rejected_ack_keeps_retrying_with_backoff(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event(record_id=9)))
    cpe.run_once()

    monkeypatch.setattr(
        agent_sync, "send_batch",
        lambda client, base_url, token, timeout, payload: {
            "accepted": 0, "duplicates": 0, "rejected": 1,
            "results": [{"record_id": 9, "status": "rejected", "error": "department not found"}],
        },
    )
    monkeypatch.setattr(agent_sync, "send_heartbeat", lambda *a, **k: {"ok": True, "server_status": "online"})
    agent_sync.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        outbox = session.query(OutboxEvent).one()
        assert outbox.status == "failed"
        assert outbox.attempts == 1
        assert "department not found" in outbox.last_error
        assert outbox.next_attempt_at is not None
    finally:
        session.close()


def test_agent_sync_network_failure_does_not_mark_delivered_and_schedules_retry(app_env, monkeypatch):
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event(record_id=3)))
    cpe.run_once()

    def _raise(*args, **kwargs):
        raise agent_sync.AgentSyncError("центр недоступен: connection refused")

    monkeypatch.setattr(agent_sync, "send_batch", _raise)
    monkeypatch.setattr(agent_sync, "send_heartbeat", lambda *a, **k: (_ for _ in ()).throw(agent_sync.AgentSyncError("недоступен")))

    # Не должно бросить исключение наружу — сбой центра не должен ронять запуск.
    agent_sync.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        outbox = session.query(OutboxEvent).one()
        assert outbox.status == "pending"
        assert outbox.delivered_at is None
        assert outbox.attempts == 1
        assert outbox.next_attempt_at is not None
        assert "недоступен" in outbox.last_error
    finally:
        session.close()


def test_agent_sync_recovers_automatically_once_center_is_back(app_env, monkeypatch):
    """Симулирует полный сценарий из критериев приёмки: отключить центр,
    собрать события локально, включить центр обратно — очередь должна
    досылаться автоматически без ручных действий и без потерь."""
    _enable_agent_mode(monkeypatch)
    import collector.agent_sync as agent_sync
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps([_one_event(1), _one_event(2, printer="HP-3F-BW")]))
    cpe.run_once()

    def _raise(*args, **kwargs):
        raise agent_sync.AgentSyncError("центр недоступен")

    monkeypatch.setattr(agent_sync, "send_batch", _raise)
    monkeypatch.setattr(agent_sync, "send_heartbeat", _raise)
    agent_sync.run_once()  # центр "выключен"

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        assert session.query(OutboxEvent).filter_by(status="delivered").count() == 0
        # Сбрасываем next_attempt_at, чтобы не ждать реального backoff в тесте.
        session.query(OutboxEvent).update({"next_attempt_at": None})
        session.commit()
    finally:
        session.close()

    def _fake_send_batch(client, base_url, token, timeout, payload):
        return {
            "accepted": len(payload["events"]), "duplicates": 0, "rejected": 0,
            "results": [{"record_id": e["record_id"], "status": "inserted"} for e in payload["events"]],
        }

    monkeypatch.setattr(agent_sync, "send_batch", _fake_send_batch)
    monkeypatch.setattr(agent_sync, "send_heartbeat", lambda *a, **k: {"ok": True, "server_status": "online"})
    agent_sync.run_once()  # центр "включили обратно"

    session = SessionLocal()
    try:
        rows = session.query(OutboxEvent).all()
        assert len(rows) == 2
        assert all(r.status == "delivered" for r in rows)
    finally:
        session.close()


def test_outbox_survives_reopening_a_new_session(app_env, monkeypatch):
    """Не буквальный перезапуск процесса, но проверяет то, что реально имеет
    значение: очередь — это строки в БД, а не состояние в памяти агента,
    поэтому новая сессия (как после перезапуска) видит её как есть."""
    _enable_agent_mode(monkeypatch)
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event(record_id=1)))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import OutboxEvent

    session = SessionLocal()
    try:
        assert session.query(OutboxEvent).filter_by(status="pending").count() == 1
    finally:
        session.close()

    # Новая сессия — как будто новый процесс после перезапуска.
    session2 = SessionLocal()
    try:
        assert session2.query(OutboxEvent).filter_by(status="pending").count() == 1
    finally:
        session2.close()


def test_agent_sync_does_nothing_when_not_agent_mode(app_env, monkeypatch, caplog):
    import collector.agent_sync as agent_sync

    # APP_MODE не задан -> standalone по умолчанию.
    called = []
    monkeypatch.setattr(agent_sync, "send_batch", lambda *a, **k: called.append(1))
    agent_sync.run_once()
    assert called == []


def test_agent_sync_skips_when_not_fully_configured(app_env, monkeypatch):
    import collector.agent_sync as agent_sync

    monkeypatch.setenv("APP_MODE", "agent")
    # CENTRAL_BASE_URL/AGENT_TOKEN и т.п. не заданы.
    called = []
    monkeypatch.setattr(agent_sync, "send_batch", lambda *a, **k: called.append(1))
    agent_sync.run_once()
    assert called == []


def test_compute_backoff_seconds_grows_and_is_capped():
    from collector.agent_sync import BACKOFF_MAX_SECONDS, compute_backoff_seconds

    small = compute_backoff_seconds(1)
    larger = compute_backoff_seconds(5)
    huge = compute_backoff_seconds(50)
    assert small < larger
    assert huge <= BACKOFF_MAX_SECONDS * (1 + 0.3)
