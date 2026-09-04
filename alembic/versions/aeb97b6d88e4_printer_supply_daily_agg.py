"""printer supply daily aggregate table (retention)

Компактный дневной тренд уровня расходника, который переживает
retention-очистку сырых printer_supply_samples (см.
printaudit.monitoring.retention, docs/PRINTER_MONITORING_FORECASTING.md).
Полностью аддитивно, новая таблица.

Revision ID: aeb97b6d88e4
Revises: 4e5a0c10c94f
Create Date: 2026-09-04 09:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'aeb97b6d88e4'
down_revision: Union[str, None] = '4e5a0c10c94f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "printer_supply_daily_agg",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "printer_device_id", sa.Integer(),
            sa.ForeignKey("printer_devices.id", name="fk_supply_daily_agg_printer_device_id"), nullable=False,
        ),
        sa.Column("supply_type", sa.String(length=40), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("min_level_percent", sa.Float(), nullable=True),
        sa.Column("avg_level_percent", sa.Float(), nullable=True),
        sa.Column("max_level_percent", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("printer_device_id", "supply_type", "day", name="uq_supply_daily_agg"),
    )
    op.create_index("ix_supply_daily_agg_printer_device_id", "printer_supply_daily_agg", ["printer_device_id"])
    op.create_index("ix_supply_daily_agg_day", "printer_supply_daily_agg", ["day"])


def downgrade() -> None:
    op.drop_index("ix_supply_daily_agg_day", table_name="printer_supply_daily_agg")
    op.drop_index("ix_supply_daily_agg_printer_device_id", table_name="printer_supply_daily_agg")
    op.drop_table("printer_supply_daily_agg")
