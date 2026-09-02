"""local auth provider fields on app_users

Добавляет локальную (не-AD) аутентификацию как второй, независимый провайдер.
Все новые колонки nullable/со server_default — существующие строки app_users
(созданные через AD-вход) автоматически получают auth_provider='ad' и
NULL во всех остальных новых полях, ничего не теряется и не требует
дополнительного backfill вручную.

Revision ID: 1eba877fed63
Revises: 4980b40c3753
Create Date: 2026-09-02 17:00:44.283419

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '1eba877fed63'
down_revision: Union[str, None] = '4980b40c3753'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_users") as batch_op:
        batch_op.add_column(sa.Column("auth_provider", sa.String(length=10), nullable=False, server_default="ad"))
        batch_op.add_column(sa.Column("password_hash", sa.String(length=300), nullable=True))
        batch_op.add_column(
            sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_users") as batch_op:
        batch_op.drop_column("password_changed_at")
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_login_count")
        batch_op.drop_column("must_change_password")
        batch_op.drop_column("password_hash")
        batch_op.drop_column("auth_provider")
