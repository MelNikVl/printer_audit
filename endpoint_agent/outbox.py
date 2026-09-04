"""Локальная durable очередь endpoint-агента — raw sqlite3 (не
SQLAlchemy/printaudit.models), чтобы не тянуть тяжёлые зависимости на
пользовательский ПК (см. endpoint_agent/__init__.py). Семантика
retryable/terminal ровно та же, что и у серверного OutboxEvent (см.
webapp/agent_api.py, collector/agent_sync.py, часть 5 требований по
hardening из предыдущей фазы): сетевые ошибки/5xx повторяются с backoff,
явно отклонённое сервером (rejected) событие уходит в терминальный failed
без бесконечных повторов, что переживает перезапуск процесса/ПК."""
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"  # терминальный статус — сервер явно отклонил событие

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_CURSOR_KEY = "last_record_id"


@dataclass
class OutboxRow:
    id: int
    record_id: int
    payload: dict
    attempts: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def get_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM agent_state WHERE key = ?", (_CURSOR_KEY,)).fetchone()
    return int(row[0]) if row else 0


def set_cursor(conn: sqlite3.Connection, record_id: int) -> None:
    conn.execute(
        "INSERT INTO agent_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_CURSOR_KEY, str(record_id)),
    )
    conn.commit()


def enqueue_events(conn: sqlite3.Connection, events: List[dict]) -> int:
    """events — список уже готовых к отправке payload-словарей (одно поле
    события EndpointEventIn), events[i]["record_id"] — уникальный ключ.
    INSERT OR IGNORE — защита от повторной постановки в очередь того же
    record_id (курсор уже должен это предотвращать в штатной работе, но
    дублирование здесь безопаснее, чем падение)."""
    inserted = 0
    now = _now_iso()
    for evt in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO outbox_events(record_id, payload_json, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (evt["record_id"], json.dumps(evt, ensure_ascii=False), STATUS_PENDING, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def fetch_due_batch(conn: sqlite3.Connection, limit: int) -> List[OutboxRow]:
    now = _now_iso()
    rows = conn.execute(
        "SELECT id, record_id, payload_json, attempts FROM outbox_events "
        "WHERE status = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY id ASC LIMIT ?",
        (STATUS_PENDING, now, limit),
    ).fetchall()
    return [OutboxRow(id=r[0], record_id=r[1], payload=json.loads(r[2]), attempts=r[3]) for r in rows]


def mark_sent(conn: sqlite3.Connection, ids: List[int]) -> None:
    if not ids:
        return
    conn.executemany("UPDATE outbox_events SET status = ? WHERE id = ?", [(STATUS_SENT, i) for i in ids])
    conn.commit()


def mark_duplicate(conn: sqlite3.Connection, ids: List[int]) -> None:
    """Сервер уже видел этот record_id (идемпотентность) — с точки зрения
    локальной очереди это тоже успех, повторять нечего."""
    mark_sent(conn, ids)


def mark_retry(conn: sqlite3.Connection, id_: int, error: str, next_attempt_at: datetime) -> None:
    conn.execute(
        "UPDATE outbox_events SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? WHERE id = ?",
        (error[:2000], next_attempt_at.isoformat(), id_),
    )
    conn.commit()


def mark_failed_terminal(conn: sqlite3.Connection, id_: int, error: str) -> None:
    conn.execute(
        "UPDATE outbox_events SET status = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
        (STATUS_FAILED, error[:2000], id_),
    )
    conn.commit()


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM outbox_events WHERE status = ?", (STATUS_PENDING,)).fetchone()[0]


def failed_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM outbox_events WHERE status = ?", (STATUS_FAILED,)).fetchone()[0]


def retry_failed(conn: sqlite3.Connection) -> int:
    """Ручной сброс терминально отклонённых событий обратно в pending —
    например, после того как администратор поправил конфигурацию площадки
    (см. collector/agent_sync.py::--retry-failed, тот же принцип)."""
    cur = conn.execute(
        "UPDATE outbox_events SET status = ?, next_attempt_at = NULL WHERE status = ?",
        (STATUS_PENDING, STATUS_FAILED),
    )
    conn.commit()
    return cur.rowcount
