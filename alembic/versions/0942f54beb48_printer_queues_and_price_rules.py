"""printer queues and price rules

Revision ID: 0942f54beb48
Revises: a6ec622f7679
Create Date: 2026-09-02 15:20:30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0942f54beb48'
down_revision: Union[str, None] = 'a6ec622f7679'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "printer_queues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printer_name", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("server_name", sa.String(length=200), nullable=True),
        sa.Column("share_name", sa.String(length=200), nullable=True),
        sa.Column("driver_name", sa.String(length=300), nullable=True),
        sa.Column("port_name", sa.String(length=200), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("printer_status", sa.String(length=50), nullable=True),
        sa.Column("color_mode", sa.String(length=10), nullable=False, server_default="unknown"),
        sa.Column("collection_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("price_per_page", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="KZT"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_job_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("discovered_by_collector", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("printer_name", name="uq_printer_queues_printer_name"),
    )
    op.create_index("ix_printer_queues_printer_name", "printer_queues", ["printer_name"])

    op.create_table(
        "price_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printer_queue_id", sa.Integer(), sa.ForeignKey("printer_queues.id"), nullable=True),
        sa.Column("is_color", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("price_per_page", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="KZT"),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_price_rules_printer_queue_id", "price_rules", ["printer_queue_id"])


def downgrade() -> None:
    op.drop_table("price_rules")
    op.drop_table("printer_queues")
