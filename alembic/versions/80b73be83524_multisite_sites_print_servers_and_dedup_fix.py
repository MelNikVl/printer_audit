"""multisite: sites, print_servers, outbox_events, dedup-key fix

Добавляет основу центральной архитектуры (см. docs/MULTISITE_ARCHITECTURE.md):

  - sites            — площадки (стабильный uuid, уникальный site_code);
  - print_servers     — регистрации Print Server/агента на площадке
                        (uuid, токен только как хэш, heartbeat/sync-метрики);
  - outbox_events     — durable outbox агента (см. Часть 4 требований).

Одновременно чинит две реальных проблемы уникальности:

  1. print_jobs: (site_code, record_id) НЕ гарантирует идемпотентность, если
     на площадке два Print Server — у каждого свой независимый EventRecordID.
     Настоящий ключ — (print_server_id, record_id). Старое ограничение
     uq_print_jobs_site_record снимается, новое uq_print_jobs_server_record
     добавляется.
  2. printer_queues.printer_name была уникальна ГЛОБАЛЬНО — одноимённые
     очереди на разных площадках/серверах ("HP-3F-BW" и там, и там) не могли
     сосуществовать. Новый ключ — (print_server_id, printer_name).

Backfill существующих данных: для каждого различного site_code, встреченного
в print_jobs, заводится Site + "унаследованный" PrintServer с именем
"LEGACY-<site_code>" (реальное имя Windows-сервера исторические данные не
хранили), и все строки print_jobs с этим site_code получают их id — без
этого миллионы уже накопленных заданий остались бы без print_server_id и не
попадали бы под новую уникальность вообще (что само по себе не ошибка, но
теряло бы точность отчётов по площадкам/серверам для истории). Если в одной
БД исторически встретилось больше одного site_code (сама архитектура ДО этой
ветки такого не подразумевала, но на всякий случай), printer_queues (у неё
нет своей site_code-колонки) не может быть однозначно разнесена по серверам —
в этом редком случае её print_server_id остаётся NULL, и в вывод `alembic
upgrade` печатается предупреждение для ручной проверки. print_jobs эта
неоднозначность не касается — там site_code известен для каждой строки.

Revision ID: 80b73be83524
Revises: 1eba877fed63
Create Date: 2026-09-03 00:00:00.000000

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '80b73be83524'
down_revision: Union[str, None] = '1eba877fed63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("site_code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("uuid", name="uq_sites_uuid"),
        sa.UniqueConstraint("site_code", name="uq_sites_site_code"),
    )
    op.create_index("ix_sites_site_code", "sites", ["site_code"])

    op.create_table(
        "print_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", name="fk_print_servers_site_id"), nullable=False),
        sa.Column("server_name", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("agent_version", sa.String(length=50), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("pending_queue_size", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("token_hash", sa.String(length=128), nullable=True),
        sa.Column("token_created_at", sa.DateTime(), nullable=True),
        sa.Column("token_rotated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("uuid", name="uq_print_servers_uuid"),
        sa.UniqueConstraint("site_id", "server_name", name="uq_print_servers_site_server"),
    )
    op.create_index("ix_print_servers_site_id", "print_servers", ["site_id"])

    # --- print_jobs: новые колонки (без ограничений уникальности пока) -----
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.add_column(sa.Column("source_computer", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("copies", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("pages_per_copy", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("color_source", sa.String(length=10), nullable=False, server_default="unknown"))
        batch_op.add_column(sa.Column("currency", sa.String(length=10), nullable=True))
        batch_op.add_column(
            sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", name="fk_print_jobs_site_id"), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "print_server_id",
                sa.Integer(),
                sa.ForeignKey("print_servers.id", name="fk_print_jobs_print_server_id"),
                nullable=True,
            )
        )
    op.create_index("ix_print_jobs_site_id", "print_jobs", ["site_id"])
    op.create_index("ix_print_jobs_print_server_id", "print_jobs", ["print_server_id"])

    # --- printer_queues: новая колонка --------------------------------------
    with op.batch_alter_table("printer_queues") as batch_op:
        batch_op.add_column(
            sa.Column(
                "print_server_id",
                sa.Integer(),
                sa.ForeignKey("print_servers.id", name="fk_printer_queues_print_server_id"),
                nullable=True,
            )
        )
    op.create_index("ix_printer_queues_print_server_id", "printer_queues", ["print_server_id"])

    # --- backfill: один Site + один "унаследованный" PrintServer на каждый
    # различный site_code, встреченный в уже накопленных print_jobs ---------
    now = datetime.now(timezone.utc)
    distinct_site_codes = [
        row[0]
        for row in bind.execute(sa.text("SELECT DISTINCT site_code FROM print_jobs")).fetchall()
        if row[0]
    ]

    server_ids_by_code = {}
    for site_code in distinct_site_codes:
        bind.execute(
            sa.text(
                "INSERT INTO sites (uuid, site_code, name, is_active, created_at) "
                "VALUES (:uuid, :site_code, :name, :is_active, :created_at)"
            ),
            {
                "uuid": str(uuid.uuid4()),
                "site_code": site_code,
                "name": site_code,
                "is_active": True,
                "created_at": now,
            },
        )
        site_id = bind.execute(
            sa.text("SELECT id FROM sites WHERE site_code = :site_code"), {"site_code": site_code}
        ).scalar()

        server_name = f"LEGACY-{site_code}"
        bind.execute(
            sa.text(
                "INSERT INTO print_servers (uuid, site_id, server_name, display_name, created_at, updated_at) "
                "VALUES (:uuid, :site_id, :server_name, :display_name, :now, :now)"
            ),
            {
                "uuid": str(uuid.uuid4()),
                "site_id": site_id,
                "server_name": server_name,
                "display_name": "Импортирован при миграции 80b73be83524 (до multisite, реальное имя сервера не сохранялось)",
                "now": now,
            },
        )
        server_id = bind.execute(
            sa.text("SELECT id FROM print_servers WHERE site_id = :site_id AND server_name = :server_name"),
            {"site_id": site_id, "server_name": server_name},
        ).scalar()
        server_ids_by_code[site_code] = server_id

        bind.execute(
            sa.text(
                "UPDATE print_jobs SET site_id = :site_id, print_server_id = :server_id WHERE site_code = :site_code"
            ),
            {"site_id": site_id, "server_id": server_id, "site_code": site_code},
        )

    if len(distinct_site_codes) == 1:
        only_server_id = server_ids_by_code[distinct_site_codes[0]]
        bind.execute(
            sa.text("UPDATE printer_queues SET print_server_id = :server_id"),
            {"server_id": only_server_id},
        )
    elif len(distinct_site_codes) > 1:
        print(
            "ВНИМАНИЕ (миграция 80b73be83524): в print_jobs встретилось несколько "
            f"разных site_code ({distinct_site_codes}). printer_queues не содержит "
            "site_code и не может быть однозначно распределена по площадкам "
            "автоматически — print_server_id для существующих очередей оставлен "
            "NULL. Проверьте /admin/printers и назначьте очереди серверам вручную, "
            "если это важно для отчётов. См. docs/MULTISITE_ARCHITECTURE.md."
        )

    # --- constraints: теперь, когда backfill прошёл, можно безопасно menять
    # ограничения уникальности ------------------------------------------------
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_constraint("uq_print_jobs_site_record", type_="unique")
        batch_op.create_unique_constraint("uq_print_jobs_server_record", ["print_server_id", "record_id"])

    with op.batch_alter_table("printer_queues") as batch_op:
        batch_op.drop_constraint("uq_printer_queues_printer_name", type_="unique")
        batch_op.create_unique_constraint("uq_printer_queues_server_name", ["print_server_id", "printer_name"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "print_job_id",
            sa.Integer(),
            sa.ForeignKey("print_jobs.id", name="fk_outbox_events_print_job_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_batch_id", sa.String(length=36), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("print_job_id", name="uq_outbox_events_print_job_id"),
    )
    op.create_index("ix_outbox_events_print_job_id", "outbox_events", ["print_job_id"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_index("ix_outbox_events_next_attempt_at", "outbox_events", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("outbox_events")

    with op.batch_alter_table("printer_queues") as batch_op:
        batch_op.drop_constraint("uq_printer_queues_server_name", type_="unique")
        batch_op.create_unique_constraint("uq_printer_queues_printer_name", ["printer_name"])

    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_constraint("uq_print_jobs_server_record", type_="unique")
        batch_op.create_unique_constraint("uq_print_jobs_site_record", ["site_code", "record_id"])

    op.drop_index("ix_printer_queues_print_server_id", table_name="printer_queues")
    with op.batch_alter_table("printer_queues") as batch_op:
        batch_op.drop_column("print_server_id")

    op.drop_index("ix_print_jobs_print_server_id", table_name="print_jobs")
    op.drop_index("ix_print_jobs_site_id", table_name="print_jobs")
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_column("print_server_id")
        batch_op.drop_column("site_id")
        batch_op.drop_column("currency")
        batch_op.drop_column("color_source")
        batch_op.drop_column("pages_per_copy")
        batch_op.drop_column("copies")
        batch_op.drop_column("source_computer")

    op.drop_index("ix_print_servers_site_id", table_name="print_servers")
    op.drop_table("print_servers")

    op.drop_index("ix_sites_site_code", table_name="sites")
    op.drop_table("sites")
