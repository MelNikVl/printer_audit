"""print_servers: last_contact_at, last_ingest_error, failed_queue_size

Pre-merge hardening (см. docs/MULTISITE_ARCHITECTURE.md, разделы про
last_sync_at и outbox): разделяет "агент достучался" (last_contact_at) от
"последний пакет успешно синхронизирован полностью" (last_sync_at, теперь
НЕ обновляется, если хоть одно событие пакета отклонено — см.
webapp/agent_api.py). last_ingest_error — отдельное от last_error поле:
last_error приходит от самого агента через heartbeat, last_ingest_error —
то, что обнаружил центр при разборе присланных событий. failed_queue_size —
раздельный счётчик терминально отклонённых событий outbox (в отличие от
pending_queue_size, который больше не должен их включать).

Все новые колонки nullable — существующие строки print_servers получают
NULL, ничего не теряется и не требует backfill.

Revision ID: d2a0ff9eb63d
Revises: 80b73be83524
Create Date: 2026-09-03 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd2a0ff9eb63d'
down_revision: Union[str, None] = '80b73be83524'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("print_servers") as batch_op:
        batch_op.add_column(sa.Column("last_contact_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("failed_queue_size", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_ingest_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("print_servers") as batch_op:
        batch_op.drop_column("last_ingest_error")
        batch_op.drop_column("failed_queue_size")
        batch_op.drop_column("last_contact_at")
