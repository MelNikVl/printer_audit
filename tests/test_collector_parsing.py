"""Тесты для бага: Export-PrintEvents.ps1 при РОВНО одном событии отдавал JSON
object вместо массива (ConvertTo-Json разворачивает пайплайн с одним
элементом), из-за чего `fetch_events()` возвращал dict, `run_once()` итерировал
его ключи как события и падал с `TypeError: string indices must be integers`,
а в лог писалось "Получено N событий" (N = число ключей dict), хотя реально
пришло одно.

Часть тестов проверяет только чистый парсинг (`parse_export_output`,
`get_field`) без БД; часть — полный `run_once()` с замоканным `subprocess.run`,
чтобы убедиться, что баг не воспроизводится на уровне всего сборщика и что
курсор не продвигается при сбое всего прогона.
"""
import json

import pytest


def test_parse_zero_events_returns_empty_list():
    from collector.collect_print_events import parse_export_output

    assert parse_export_output("[]") == []
    assert parse_export_output("") == []
    assert parse_export_output("   ") == []


def test_parse_single_event_object_is_normalized_to_list():
    """Воспроизводит сам баг: PowerShell отдал JSON-объект (не массив) для
    единственного события — Python должен аккуратно завернуть его в список,
    а не итерировать его ключи."""
    from collector.collect_print_events import parse_export_output

    raw = json.dumps(
        {
            "RecordId": 42,
            "TimeCreated": "2026-09-02T10:00:00.000Z",
            "Message": "test",
            "Properties": ["a", "b"],
        }
    )
    result = parse_export_output(raw)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["RecordId"] == 42


def test_parse_multiple_events_stays_a_list():
    from collector.collect_print_events import parse_export_output

    raw = json.dumps([{"RecordId": 1}, {"RecordId": 2}, {"RecordId": 3}])
    result = parse_export_output(raw)
    assert isinstance(result, list)
    assert [e["RecordId"] for e in result] == [1, 2, 3]


def test_parse_malformed_json_raises_clear_runtime_error():
    from collector.collect_print_events import parse_export_output

    with pytest.raises(RuntimeError, match="невалидный JSON"):
        parse_export_output("{not valid json!!!")


def test_parse_unexpected_top_level_type_raises_runtime_error():
    from collector.collect_print_events import parse_export_output

    with pytest.raises(RuntimeError, match="неожиданного типа"):
        parse_export_output("42")


def test_parse_list_with_non_object_item_raises_runtime_error():
    from collector.collect_print_events import parse_export_output

    with pytest.raises(RuntimeError, match="элемент #1"):
        parse_export_output(json.dumps([{"RecordId": 1}, "garbage"]))


def test_get_field_not_configured_returns_default():
    from collector.collect_print_events import get_field

    assert get_field(["a", "b"], {}, "job_id", default=None) is None
    assert get_field(["a", "b"], {}, "job_id", default="x") == "x"


def test_get_field_in_range_returns_value():
    from collector.collect_print_events import get_field

    props = ["a", "b", "c"]
    assert get_field(props, {"user_name": 1}, "user_name") == "b"


def test_get_field_out_of_range_raises_field_map_error_with_diagnostics():
    from collector.collect_print_events import FieldMapError, get_field

    props = ["a", "b", "c"]  # индексы 0..2
    with pytest.raises(FieldMapError) as exc_info:
        get_field(props, {"total_pages": 8}, "total_pages")
    message = str(exc_info.value)
    assert "total_pages=8" in message
    assert "3 свойств" in message
    assert "calibrate_event_fields.ps1" in message


def test_get_field_negative_index_raises_field_map_error():
    from collector.collect_print_events import FieldMapError, get_field

    with pytest.raises(FieldMapError):
        get_field(["a"], {"total_pages": -1}, "total_pages")


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, module, stdout, returncode=0, stderr=""):
    def _fake_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return _FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(module.subprocess, "run", _fake_run)


def test_run_once_single_event_from_object_output_inserts_one_job(app_env, monkeypatch):
    """Полный прогон run_once(), когда PowerShell (как в реальном баге) отдаёт
    JSON-объект вместо массива для единственного события — должно вставиться
    ровно одно задание, без TypeError и без раздутого счётчика "событий"."""
    import collector.collect_print_events as cpe

    single_event_object = {
        "RecordId": 501,
        "TimeCreated": "2026-09-02T10:15:00.000Z",
        "Message": "job",
        # индексы по умолчанию из field_map: job_id=0, document_name=1, user_name=2, printer_name=4, total_pages=8
        "Properties": ["1001", "report.docx", "DOMAIN\\ivanov", "x", "HP-3F-BW", "y", "z", "w", 5],
    }
    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(single_event_object))

    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import CollectorState, PrintJob

    session = SessionLocal()
    try:
        jobs = session.query(PrintJob).all()
        assert len(jobs) == 1
        assert jobs[0].record_id == 501
        assert jobs[0].total_pages == 5
        assert jobs[0].user_name == "DOMAIN\\ivanov"

        state = session.get(CollectorState, "TEST")
        assert state.last_record_id == 501
    finally:
        session.close()


def test_run_once_zero_events_leaves_cursor_unchanged(app_env, monkeypatch):
    import collector.collect_print_events as cpe

    _patch_subprocess(monkeypatch, cpe, stdout="[]")
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import CollectorState

    session = SessionLocal()
    try:
        state = session.get(CollectorState, "TEST")
        assert state.last_record_id == 0
    finally:
        session.close()


def test_run_once_multiple_events_all_inserted(app_env, monkeypatch):
    import collector.collect_print_events as cpe

    events = [
        {
            "RecordId": i,
            "TimeCreated": "2026-09-02T10:00:00.000Z",
            "Message": "job",
            "Properties": [str(i), f"doc{i}.pdf", "DOMAIN\\ivanov", "x", "HP-3F-BW", "y", "z", "w", 2],
        }
        for i in (10, 11, 12)
    ]
    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(events))
    cpe.run_once()

    from printaudit.database import SessionLocal
    from printaudit.models import CollectorState, PrintJob

    session = SessionLocal()
    try:
        assert session.query(PrintJob).count() == 3
        state = session.get(CollectorState, "TEST")
        assert state.last_record_id == 12
    finally:
        session.close()


def test_run_once_does_not_advance_cursor_when_fetch_fails(app_env, monkeypatch):
    """Если Export-PrintEvents.ps1 вернул невалидный JSON (или иначе сломался),
    весь прогон должен провалиться И курсор last_record_id не должен сдвинуться —
    иначе потерянные события никогда не будут переобработаны."""
    import collector.collect_print_events as cpe
    from printaudit.database import SessionLocal
    from printaudit.models import CollectorState

    _patch_subprocess(monkeypatch, cpe, stdout="{not valid json")

    with pytest.raises(RuntimeError, match="невалидный JSON"):
        cpe.run_once()

    session = SessionLocal()
    try:
        state = session.get(CollectorState, "TEST")
        assert state.last_record_id == 0
        assert state.last_run_at is None
    finally:
        session.close()


def test_run_once_is_idempotent_on_duplicate_record_id(app_env, monkeypatch):
    """Повторный запуск с тем же RecordId (например, если после успешной вставки
    и до продвижения курсора процесс был прерван) не должен создавать дубли —
    основа идемпотентности сборщика."""
    import collector.collect_print_events as cpe
    from printaudit.database import SessionLocal
    from printaudit.models import CollectorState, PrintJob

    event = {
        "RecordId": 900,
        "TimeCreated": "2026-09-02T10:00:00.000Z",
        "Message": "job",
        "Properties": ["1", "doc.pdf", "DOMAIN\\ivanov", "x", "HP-3F-BW", "y", "z", "w", 3],
    }
    _patch_subprocess(monkeypatch, cpe, stdout=json.dumps(event))
    cpe.run_once()

    # Курсор уже продвинулся за RecordId=900, поэтому имитируем повторную
    # обработку того же события (например, ручной повторный запуск до
    # продвижения курсора на другом объекте) — сбрасываем курсор вручную и
    # прогоняем ещё раз то же самое событие.
    session = SessionLocal()
    try:
        state = session.get(CollectorState, "TEST")
        state.last_record_id = 0
        session.commit()
    finally:
        session.close()

    cpe.run_once()

    session = SessionLocal()
    try:
        assert session.query(PrintJob).filter_by(record_id=900).count() == 1
    finally:
        session.close()
