"""monitoring sync state (site -> center cursor for printer monitoring)

Курсор отправки мониторинговых сэмплов/алертов площадка -> центр, отдельный
от collector_state (курсор заданий печати) и от outbox_events (durable
outbox заданий печати) — мониторинговые данные синхронизируются курсором
по id/updated_at, не полноценным outbox с состояниями pending/failed (см.
docs/PRINTER_MONITORING_FORECASTING.md за обоснованием). Полностью
аддитивно, новая таблица.

Revision ID: ee90f44a0772
Revises: aeb97b6d88e4
Create Date: 2026-09-04 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'ee90f44a0772'
down_revision: Union[str, None] = 'aeb97b6d88e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monitoring_sync_state",
        sa.Column(
            "site_id", sa.Integer(),
            sa.ForeignKey("sites.id", name="fk_monitoring_sync_state_site_id"), primary_key=True,
        ),
        sa.Column("last_health_sample_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_counter_sample_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_supply_sample_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_alert_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("monitoring_sync_state")
