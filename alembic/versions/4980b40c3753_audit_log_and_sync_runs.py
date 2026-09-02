"""audit log and sync runs

Revision ID: 4980b40c3753
Revises: 2948ecd75b55
Create Date: 2026-09-02 15:21:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '4980b40c3753'
down_revision: Union[str, None] = '2948ecd75b55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_app_user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("object_type", sa.String(length=100), nullable=False),
        sa.Column("object_id", sa.String(length=100), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_app_user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_object_type", "audit_log", ["object_type"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_type", sa.String(length=30), nullable=False),
        sa.Column("site_code", sa.String(length=50), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("events_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_sync_runs_run_type", "sync_runs", ["run_type"])
    op.create_index("ix_sync_runs_site_code", "sync_runs", ["site_code"])


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("audit_log")
