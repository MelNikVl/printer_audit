"""active directory cache and rules (ad_groups, ad_department_rules, ad_users, ad_group_memberships)

Порядок создания важен из-за внешних ключей: ad_groups -> ad_department_rules
(group+department) -> ad_users (department + department_rule) -> ad_group_memberships
(group + user).

Revision ID: a6ec622f7679
Revises: 023a55dd3d07
Create Date: 2026-09-02 15:20:15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a6ec622f7679'
down_revision: Union[str, None] = '023a55dd3d07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ad_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dn", sa.String(length=500), nullable=False),
        sa.Column("sam_account_name", sa.String(length=200), nullable=True),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("dn", name="uq_ad_groups_dn"),
    )

    op.create_table(
        "ad_department_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ad_group_id", sa.Integer(), sa.ForeignKey("ad_groups.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ad_group_id", name="uq_ad_department_rules_group"),
    )

    op.create_table(
        "ad_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sid", sa.String(length=200), nullable=True),
        sa.Column("object_guid", sa.String(length=64), nullable=True),
        sa.Column("sam_account_name", sa.String(length=200), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=True),
        sa.Column("login_normalized", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("email", sa.String(length=300), nullable=True),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("department_source", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("department_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("department_rule_id", sa.Integer(), sa.ForeignKey("ad_department_rules.id"), nullable=True),
        sa.Column("is_ad_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("local_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("sid", name="uq_ad_users_sid"),
        sa.UniqueConstraint("object_guid", name="uq_ad_users_object_guid"),
        sa.UniqueConstraint("login_normalized", name="uq_ad_users_login_normalized"),
    )
    op.create_index("ix_ad_users_login_normalized", "ad_users", ["login_normalized"])
    op.create_index("ix_ad_users_department_id", "ad_users", ["department_id"])

    op.create_table(
        "ad_group_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ad_group_id", sa.Integer(), sa.ForeignKey("ad_groups.id"), nullable=False),
        sa.Column("ad_user_id", sa.Integer(), sa.ForeignKey("ad_users.id"), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ad_group_id", "ad_user_id", name="uq_ad_group_membership"),
    )
    op.create_index("ix_ad_group_memberships_group", "ad_group_memberships", ["ad_group_id"])
    op.create_index("ix_ad_group_memberships_user", "ad_group_memberships", ["ad_user_id"])


def downgrade() -> None:
    op.drop_table("ad_group_memberships")
    op.drop_table("ad_users")
    op.drop_table("ad_department_rules")
    op.drop_table("ad_groups")
