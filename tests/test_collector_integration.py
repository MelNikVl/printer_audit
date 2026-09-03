"""Тесты интеграции коллектора с новыми резолверами: очередь печати
создаётся автоматически при первом задании, отдел резолвится через AD (с
fallback на легаси users), и каждый прогон пишет строку в sync_runs."""
import json


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


def test_unseen_printer_creates_discovered_printer_queue(app_env, monkeypatch):
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event()))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue, PrintJob

    session = SessionLocal()
    try:
        queue = session.query(PrinterQueue).filter_by(printer_name="HP-3F-BW").one()
        assert queue.discovered_by_collector is True
        assert queue.color_mode == "unknown"
        assert queue.collection_enabled is True

        job = session.query(PrintJob).filter_by(record_id=1).one()
        assert job.printer_queue_id == queue.id
        assert job.user_login_normalized == "domain\\ivanov"
    finally:
        session.close()


def test_disabled_printer_queue_skips_new_jobs(app_env, monkeypatch):
    import collector.collect_print_events as cpe
    from printaudit.database import SessionLocal
    from printaudit.models import PrinterQueue
    from printaudit.sites import get_or_create_local_print_server

    session = SessionLocal()
    # Очередь должна быть привязана к тому же (авто-заведённому) локальному
    # print_server, который коллектор будет искать при следующем прогоне —
    # см. printaudit.models.PrinterQueue.uq_printer_queues_server_name.
    print_server = get_or_create_local_print_server(session)
    session.add(
        PrinterQueue(
            printer_name="HP-3F-BW", print_server_id=print_server.id,
            display_name="HP-3F-BW", collection_enabled=False,
        )
    )
    session.commit()
    session.close()

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event()))
    cpe.run_once()

    session = SessionLocal()
    try:
        from printaudit.models import PrintJob

        assert session.query(PrintJob).count() == 0
    finally:
        session.close()


def test_job_department_resolved_via_ad_user(app_env, monkeypatch):
    import collector.collect_print_events as cpe
    from printaudit.database import SessionLocal
    from printaudit.models import AdUser, Department

    session = SessionLocal()
    dept = Department(name="Бухгалтерия")
    session.add(dept)
    session.flush()
    session.add(AdUser(sam_account_name="ivanov", login_normalized="domain\\ivanov", department_id=dept.id))
    session.commit()
    dept_id = dept.id
    session.close()

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event()))
    cpe.run_once()

    session = SessionLocal()
    try:
        from printaudit.models import PrintJob

        job = session.query(PrintJob).filter_by(record_id=1).one()
        assert job.department_id == dept_id
    finally:
        session.close()


def test_successful_run_writes_sync_run_row(app_env, monkeypatch):
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps([_one_event(1), _one_event(2, printer="HP-3F-BW")]))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import SyncRun

    session = SessionLocal()
    try:
        runs = session.query(SyncRun).filter_by(run_type="collector").all()
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].inserted == 2
        assert runs[0].events_fetched == 2
    finally:
        session.close()


def test_failed_run_writes_failed_sync_run_row_and_no_cursor_advance(app_env, monkeypatch):
    import collector.collect_print_events as cpe
    import pytest

    _patch_subprocess(monkeypatch, cpe, stdout="{not valid json")

    with pytest.raises(RuntimeError):
        cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import CollectorState, SyncRun

    session = SessionLocal()
    try:
        runs = session.query(SyncRun).filter_by(run_type="collector").all()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].error_message

        state = session.get(CollectorState, "TEST")
        assert state.last_record_id == 0
    finally:
        session.close()


def test_document_name_masked_policy_applied(app_env, monkeypatch):
    import collector.collect_print_events as cpe
    from printaudit.config import get_settings

    get_settings().document_name_policy = "masked"
    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(_one_event()))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import PrintJob

    session = SessionLocal()
    try:
        job = session.query(PrintJob).filter_by(record_id=1).one()
        assert job.document_name == "•••.docx"
    finally:
        session.close()
