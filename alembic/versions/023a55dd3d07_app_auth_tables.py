"""app auth tables (app_users, web_sessions)

Revision ID: 023a55dd3d07
Revises: 90fa7d836021
Create Date: 2026-09-02 15:20:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '023a55dd3d07'
down_revision: Union[str, None] = '90fa7d836021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ad_sid", sa.String(length=200), nullable=True),
        sa.Column("ad_object_guid", sa.String(length=64), nullable=True),
        sa.Column("login_normalized", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("email", sa.String(length=300), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("assigned_by_id", sa.Integer(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ad_sid", name="uq_app_users_ad_sid"),
        sa.UniqueConstraint("ad_object_guid", name="uq_app_users_ad_object_guid"),
        sa.UniqueConstraint("login_normalized", name="uq_app_users_login_normalized"),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["app_users.id"], name="fk_app_users_assigned_by"),
        sa.ForeignKeyConstraint(["disabled_by_id"], ["app_users.id"], name="fk_app_users_disabled_by"),
    )
    op.create_index("ix_app_users_login_normalized", "app_users", ["login_normalized"])

    op.create_table(
        "web_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("app_user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_web_sessions_app_user_id", "web_sessions", ["app_user_id"])
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("web_sessions")
    op.drop_table("app_users")
