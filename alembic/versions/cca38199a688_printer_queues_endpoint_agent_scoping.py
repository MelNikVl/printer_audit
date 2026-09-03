"""printer_queues.endpoint_agent_id (dual scoping like print_jobs)

Endpoint-агенты (USB/прямые IP-принтеры на пользовательских ПК, см.
docs/PRINTER_MONITORING_FORECASTING.md) не привязаны ни к одному Print
Server — их локальные принтеры нужно масштабировать так же, как и
серверные очереди: printer_queues.printer_name повторяется между разными
ПК (одинаковая модель USB-принтера часто называется у Windows одинаково).
Добавляет printer_queues.endpoint_agent_id (nullable, как и
print_server_id) и новый UNIQUE(endpoint_agent_id, printer_name) —
ровно один из двух "источников" заполнен на строку, никогда оба сразу
(тот же принцип, что и print_jobs.print_server_id/endpoint_agent_id).

Полностью аддитивно: существующие строки printer_queues получают
endpoint_agent_id=NULL, ничего не теряется, старый
uq_printer_queues_server_name не трогается.

Revision ID: cca38199a688
Revises: ee90f44a0772
Create Date: 2026-09-04 10:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'cca38199a688'
down_revision: Union[str, None] = 'ee90f44a0772'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("printer_queues") as batch_op:
        batch_op.add_column(
            sa.Column(
                "endpoint_agent_id", sa.Integer(),
                sa.ForeignKey("endpoint_agents.id", name="fk_printer_queues_endpoint_agent_id"), nullable=True,
            )
        )
        batch_op.create_unique_constraint("uq_printer_queues_endpoint_name", ["endpoint_agent_id", "printer_name"])
    op.create_index("ix_printer_queues_endpoint_agent_id", "printer_queues", ["endpoint_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_printer_queues_endpoint_agent_id", table_name="printer_queues")
    with op.batch_alter_table("printer_queues") as batch_op:
        batch_op.drop_constraint("uq_printer_queues_endpoint_name", type_="unique")
        batch_op.drop_column("endpoint_agent_id")
