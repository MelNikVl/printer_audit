"""endpoint_agent.outbox — локальная durable очередь (raw sqlite3):
идемпотентная постановка, курсор, retryable vs терминальный failed,
ручной сброс failed->pending."""
from datetime import datetime, timedelta, timezone

from endpoint_agent import outbox


def _conn(tmp_path):
    return outbox.open_db(tmp_path / "outbox.sqlite3")


def test_cursor_defaults_to_zero_and_persists(tmp_path):
    conn = _conn(tmp_path)
    assert outbox.get_cursor(conn) == 0
    outbox.set_cursor(conn, 42)
    assert outbox.get_cursor(conn) == 42


def test_enqueue_is_idempotent_by_record_id(tmp_path):
    conn = _conn(tmp_path)
    events = [{"record_id": 1, "printer_name": "HP"}]
    assert outbox.enqueue_events(conn, events) == 1
    assert outbox.enqueue_events(conn, events) == 0  # уже в очереди, дубль игнорируется
    assert outbox.pending_count(conn) == 1


def test_fetch_due_batch_respects_limit_and_order(tmp_path):
    conn = _conn(tmp_path)
    outbox.enqueue_events(conn, [{"record_id": i} for i in range(5)])
    batch = outbox.fetch_due_batch(conn, limit=3)
    assert [row.record_id for row in batch] == [0, 1, 2]


def test_fetch_due_batch_excludes_not_yet_due_retries(tmp_path):
    conn = _conn(tmp_path)
    outbox.enqueue_events(conn, [{"record_id": 1}])
    row = outbox.fetch_due_batch(conn, 10)[0]
    outbox.mark_retry(conn, row.id, "network error", datetime.now(timezone.utc) + timedelta(hours=1))
    assert outbox.fetch_due_batch(conn, 10) == []


def test_fetch_due_batch_includes_due_retries(tmp_path):
    conn = _conn(tmp_path)
    outbox.enqueue_events(conn, [{"record_id": 1}])
    row = outbox.fetch_due_batch(conn, 10)[0]
    outbox.mark_retry(conn, row.id, "network error", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert len(outbox.fetch_due_batch(conn, 10)) == 1


def test_mark_sent_removes_from_pending(tmp_path):
    conn = _conn(tmp_path)
    outbox.enqueue_events(conn, [{"record_id": 1}])
    row = outbox.fetch_due_batch(conn, 10)[0]
    outbox.mark_sent(conn, [row.id])
    assert outbox.pending_count(conn) == 0
    assert outbox.fetch_due_batch(conn, 10) == []


def test_mark_failed_terminal_excluded_from_pending_and_due(tmp_path):
    conn = _conn(tmp_path)
    outbox.enqueue_events(conn, [{"record_id": 1}])
    row = outbox.fetch_due_batch(conn, 10)[0]
    outbox.mark_failed_terminal(conn, row.id, "rejected: invalid printer")
    assert outbox.pending_count(conn) == 0
    assert outbox.failed_count(conn) == 1
    assert outbox.fetch_due_batch(conn, 10) == []  # terminal failed никогда не выбирается автоматически


def test_retry_failed_resets_terminal_rows_to_pending(tmp_path):
    conn = _conn(tmp_path)
    outbox.enqueue_events(conn, [{"record_id": 1}])
    row = outbox.fetch_due_batch(conn, 10)[0]
    outbox.mark_failed_terminal(conn, row.id, "rejected")
    reset_count = outbox.retry_failed(conn)
    assert reset_count == 1
    assert outbox.failed_count(conn) == 0
    assert outbox.pending_count(conn) == 1
    assert len(outbox.fetch_due_batch(conn, 10)) == 1


def test_outbox_survives_reopen(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    conn = outbox.open_db(path)
    outbox.enqueue_events(conn, [{"record_id": 1}])
    outbox.set_cursor(conn, 99)
    conn.close()

    conn2 = outbox.open_db(path)
    assert outbox.get_cursor(conn2) == 99
    assert outbox.pending_count(conn2) == 1
