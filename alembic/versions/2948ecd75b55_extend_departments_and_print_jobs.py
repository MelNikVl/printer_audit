"""extend departments (description/is_active/display_order) and print_jobs
(user_login_normalized, printer_queue_id, price_rule_id)

Все новые колонки nullable / со server_default — существующие строки
departments и print_jobs (в т.ч. уже накопленная история печати) не теряются
и не требуют backfill для прохождения миграции. user_login_normalized для уже
существующих print_jobs остаётся NULL до следующего запуска коллектора (он
не пересматривает старые события) - это не влияет на отчёты, использующие
user_name, только на будущее сопоставление с ad_users.

Revision ID: 2948ecd75b55
Revises: 0942f54beb48
Create Date: 2026-09-02 15:20:45

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '2948ecd75b55'
down_revision: Union[str, None] = '0942f54beb48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("departments") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.add_column(sa.Column("user_login_normalized", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column(
                "printer_queue_id",
                sa.Integer(),
                sa.ForeignKey("printer_queues.id", name="fk_print_jobs_printer_queue_id"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "price_rule_id",
                sa.Integer(),
                sa.ForeignKey("price_rules.id", name="fk_print_jobs_price_rule_id"),
                nullable=True,
            )
        )

    op.create_index("ix_print_jobs_user_login_normalized", "print_jobs", ["user_login_normalized"])
    op.create_index("ix_print_jobs_printer_queue_id", "print_jobs", ["printer_queue_id"])


def downgrade() -> None:
    op.drop_index("ix_print_jobs_printer_queue_id", table_name="print_jobs")
    op.drop_index("ix_print_jobs_user_login_normalized", table_name="print_jobs")

    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_column("price_rule_id")
        batch_op.drop_column("printer_queue_id")
        batch_op.drop_column("user_login_normalized")

    with op.batch_alter_table("departments") as batch_op:
        batch_op.drop_column("display_order")
        batch_op.drop_column("is_active")
        batch_op.drop_column("description")
