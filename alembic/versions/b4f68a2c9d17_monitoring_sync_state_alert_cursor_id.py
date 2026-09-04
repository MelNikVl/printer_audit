"""monitoring_sync_state.last_alert_synced_id (composite alert cursor)

Регрессия: курсор синхронизации алертов площадка->центр продвигался
только по PrinterAlert.updated_at. Если несколько алертов получают
одинаковый updated_at (типично: один опрос устройства создаёт/обновляет
несколько строк почти в одну и ту же метку времени) и лимит пакета меньше
числа таких строк с этим updated_at, следующий запрос
(`updated_at > cursor`) навсегда пропускал бы оставшиеся строки с тем же
updated_at — они никогда не попадали бы в центр.

Добавляет last_alert_synced_id (составная часть курсора вместе с
last_alert_synced_at, см. printaudit/models.py::MonitoringSyncState и
collector/agent_sync.py::_build_monitoring_payload за использованием).
NOT NULL DEFAULT 0 — безопасно для существующих строк monitoring_sync_state
(0 корректно сочетается с last_alert_synced_at=NULL/старым значением: при
следующей синхронизации выберутся все алерты с updated_at > cursor_at ЛИБО
(updated_at == cursor_at AND id > 0), что не теряет и не дублирует ничего
по сравнению с состоянием до этой миграции).

Revision ID: b4f68a2c9d17
Revises: 7c9d3e1a5b02
Create Date: 2026-09-05 09:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4f68a2c9d17'
down_revision: Union[str, None] = '7c9d3e1a5b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("monitoring_sync_state") as batch_op:
        batch_op.add_column(
            sa.Column("last_alert_synced_id", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("monitoring_sync_state") as batch_op:
        batch_op.drop_column("last_alert_synced_id")
