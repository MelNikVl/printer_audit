"""baseline existing mvp schema

Отражает схему, которую MVP-версия проекта создавала через
`scripts/init_db.py` (SQLAlchemy `Base.metadata.create_all`), ДО этой ветки:
departments, users, price_list, print_jobs, collector_state.

Это первая ревизия Alembic в проекте, а не первый способ создания этих
таблиц — на объектах уже могут быть развёрнутые БД, созданные старым
`init_db.py`. Поэтому каждая таблица создаётся только если её ещё нет
(проверка через inspector), и `alembic upgrade head` безопасно применяется
как к чистой БД, так и к уже существующей — без требования её удалять.

Revision ID: 90fa7d836021
Revises:
Create Date: 2026-09-02 15:19:44.664720

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '90fa7d836021'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "departments" not in existing:
        op.create_table(
            "departments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("cost_center_code", sa.String(length=50), nullable=True),
            sa.UniqueConstraint("name", name="uq_departments_name"),
        )

    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("user_name", sa.String(length=200), primary_key=True),
            sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    if "price_list" not in existing:
        op.create_table(
            "price_list",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("printer_name_pattern", sa.String(length=200), nullable=False),
            sa.Column("is_color", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("price_per_page", sa.Float(), nullable=False),
            sa.Column("currency", sa.String(length=10), nullable=False, server_default="KZT"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        )

    if "print_jobs" not in existing:
        op.create_table(
            "print_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site_code", sa.String(length=50), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.String(length=50), nullable=True),
            sa.Column("time_created", sa.DateTime(), nullable=False),
            sa.Column("user_name", sa.String(length=200), nullable=False),
            sa.Column("document_name", sa.String(length=500), nullable=True),
            sa.Column("printer_name", sa.String(length=200), nullable=False),
            sa.Column("total_pages", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_color", sa.Boolean(), nullable=True),
            sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
            sa.Column("price_per_page", sa.Float(), nullable=True),
            sa.Column("cost", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("site_code", "record_id", name="uq_print_jobs_site_record"),
        )
        op.create_index("ix_print_jobs_time_created", "print_jobs", ["time_created"])
        op.create_index("ix_print_jobs_user_name", "print_jobs", ["user_name"])
        op.create_index("ix_print_jobs_printer_name", "print_jobs", ["printer_name"])
        op.create_index("ix_print_jobs_department_id", "print_jobs", ["department_id"])
        op.create_index("ix_print_jobs_site_code", "print_jobs", ["site_code"])

    if "collector_state" not in existing:
        op.create_table(
            "collector_state",
            sa.Column("site_code", sa.String(length=50), primary_key=True),
            sa.Column("last_record_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    # Намеренно не откатываем: это единственная ревизия, которая может
    # соответствовать БД с реальными накопленными print_jobs. Откат должен
    # быть осознанным ручным действием (бэкап + DROP), а не однокликовым
    # `alembic downgrade`.
    raise RuntimeError(
        "Откат базовой ревизии не реализован намеренно - это удалило бы "
        "существующие print_jobs. Восстанавливайте БД из резервной копии."
    )
