"""printer devices, monitoring samples, endpoint agents, forecasting

Добавляет основу для мониторинга физических принтеров, endpoint-агентов
(USB/прямая печать на пользовательских ПК) и прогнозирования — см.
docs/PRINTER_MONITORING_FORECASTING.md. Полностью аддитивно: все новые
таблицы, новые nullable-колонки на print_jobs — ничего из существующей
схемы не меняется и не удаляется, старые данные не трогаются.

Новые таблицы:
  - snmp_profiles              — переиспользуемые наборы OID по модели/вендору
  - printer_devices            — физические устройства (не то же самое, что
                                  printer_queues — см. models.PrinterDevice)
  - printer_device_queue_links — управляемая связь устройство<->очередь
  - monitoring_runs            — история прогонов опроса (аналог sync_runs)
  - printer_health_samples     — доступность/статус/флаги ошибок
  - printer_counter_samples    — аппаратные счётчики страниц устройства
  - printer_supply_samples     — уровни расходников (NULL=неизвестно, не 0)
  - printer_alerts             — активные/закрытые проблемы устройства
  - endpoint_agents            — регистрации endpoint-агентов на площадке
  - forecast_runs              — персистентные результаты прогнозов

print_jobs получает новую nullable-колонку endpoint_agent_id и новый
UNIQUE(endpoint_agent_id, record_id) — идемпотентность для заданий,
пришедших с endpoint-агента (USB/прямая печать), по той же схеме, что и
uq_print_jobs_server_record для Print Server (см. models.PrintJob).

Revision ID: 4e5a0c10c94f
Revises: d2a0ff9eb63d
Create Date: 2026-09-04 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '4e5a0c10c94f'
down_revision: Union[str, None] = 'd2a0ff9eb63d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "snmp_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("snmp_version", sa.String(length=10), nullable=False, server_default="v3"),
        sa.Column("credentials_env_var", sa.String(length=200), nullable=True),
        sa.Column("port", sa.Integer(), nullable=False, server_default="161"),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="2.0"),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("oid_map_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_snmp_profiles_name"),
    )

    op.create_table(
        "printer_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", name="fk_printer_devices_site_id"), nullable=False),
        sa.Column(
            "print_server_id", sa.Integer(),
            sa.ForeignKey("print_servers.id", name="fk_printer_devices_print_server_id"), nullable=True,
        ),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("mac_address", sa.String(length=32), nullable=True),
        sa.Column("serial_number", sa.String(length=100), nullable=True),
        sa.Column("vendor", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column(
            "snmp_profile_id", sa.Integer(),
            sa.ForeignKey("snmp_profiles.id", name="fk_printer_devices_snmp_profile_id"), nullable=True,
        ),
        sa.Column("monitoring_source", sa.String(length=20), nullable=False, server_default="disabled"),
        sa.Column("zabbix_host_id", sa.String(length=100), nullable=True),
        sa.Column("last_status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("uuid", name="uq_printer_devices_uuid"),
    )
    op.create_index("ix_printer_devices_site_id", "printer_devices", ["site_id"])
    op.create_index("ix_printer_devices_print_server_id", "printer_devices", ["print_server_id"])

    op.create_table(
        "printer_device_queue_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "printer_device_id", sa.Integer(),
            sa.ForeignKey("printer_devices.id", name="fk_pdql_printer_device_id"), nullable=False,
        ),
        sa.Column(
            "printer_queue_id", sa.Integer(),
            sa.ForeignKey("printer_queues.id", name="fk_pdql_printer_queue_id"), nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("linked_by_id", sa.Integer(), sa.ForeignKey("app_users.id", name="fk_pdql_linked_by_id"), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.Column(
            "unlinked_by_id", sa.Integer(), sa.ForeignKey("app_users.id", name="fk_pdql_unlinked_by_id"), nullable=True,
        ),
        sa.Column("unlinked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("printer_device_id", "printer_queue_id", name="uq_device_queue_link"),
    )
    op.create_index("ix_pdql_printer_device_id", "printer_device_queue_links", ["printer_device_id"])
    op.create_index("ix_pdql_printer_queue_id", "printer_device_queue_links", ["printer_queue_id"])

    op.create_table(
        "monitoring_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", name="fk_monitoring_runs_site_id"), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("devices_polled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("devices_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("devices_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_monitoring_runs_site_id", "monitoring_runs", ["site_id"])
    op.create_index("ix_monitoring_runs_source", "monitoring_runs", ["source"])

    op.create_table(
        "printer_health_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "printer_device_id", sa.Integer(),
            sa.ForeignKey("printer_devices.id", name="fk_health_samples_printer_device_id"), nullable=False,
        ),
        sa.Column(
            "monitoring_run_id", sa.Integer(),
            sa.ForeignKey("monitoring_runs.id", name="fk_health_samples_monitoring_run_id"), nullable=True,
        ),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("is_reachable", sa.Boolean(), nullable=True),
        sa.Column("device_status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("has_paper_jam", sa.Boolean(), nullable=True),
        sa.Column("has_cover_open", sa.Boolean(), nullable=True),
        sa.Column("has_paper_out", sa.Boolean(), nullable=True),
        sa.Column("has_hardware_error", sa.Boolean(), nullable=True),
        sa.Column("raw_status_text", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("printer_device_id", "collected_at", "source", name="uq_health_sample"),
    )
    op.create_index("ix_health_samples_printer_device_id", "printer_health_samples", ["printer_device_id"])
    op.create_index("ix_health_samples_monitoring_run_id", "printer_health_samples", ["monitoring_run_id"])
    op.create_index("ix_health_samples_collected_at", "printer_health_samples", ["collected_at"])

    op.create_table(
        "printer_counter_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "printer_device_id", sa.Integer(),
            sa.ForeignKey("printer_devices.id", name="fk_counter_samples_printer_device_id"), nullable=False,
        ),
        sa.Column(
            "monitoring_run_id", sa.Integer(),
            sa.ForeignKey("monitoring_runs.id", name="fk_counter_samples_monitoring_run_id"), nullable=True,
        ),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("total_pages", sa.Integer(), nullable=True),
        sa.Column("color_pages", sa.Integer(), nullable=True),
        sa.Column("bw_pages", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("printer_device_id", "collected_at", "source", name="uq_counter_sample"),
    )
    op.create_index("ix_counter_samples_printer_device_id", "printer_counter_samples", ["printer_device_id"])
    op.create_index("ix_counter_samples_collected_at", "printer_counter_samples", ["collected_at"])

    op.create_table(
        "printer_supply_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "printer_device_id", sa.Integer(),
            sa.ForeignKey("printer_devices.id", name="fk_supply_samples_printer_device_id"), nullable=False,
        ),
        sa.Column(
            "monitoring_run_id", sa.Integer(),
            sa.ForeignKey("monitoring_runs.id", name="fk_supply_samples_monitoring_run_id"), nullable=True,
        ),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("supply_type", sa.String(length=40), nullable=False),
        sa.Column("level_percent", sa.Float(), nullable=True),
        sa.Column("level_status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("printer_device_id", "collected_at", "source", "supply_type", name="uq_supply_sample"),
    )
    op.create_index("ix_supply_samples_printer_device_id", "printer_supply_samples", ["printer_device_id"])
    op.create_index("ix_supply_samples_collected_at", "printer_supply_samples", ["collected_at"])

    op.create_table(
        "printer_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "printer_device_id", sa.Integer(),
            sa.ForeignKey("printer_devices.id", name="fk_printer_alerts_printer_device_id"), nullable=False,
        ),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("alert_type", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="warning"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("printer_device_id", "alert_type", "external_id", name="uq_printer_alert"),
    )
    op.create_index("ix_printer_alerts_printer_device_id", "printer_alerts", ["printer_device_id"])
    op.create_index("ix_printer_alerts_alert_type", "printer_alerts", ["alert_type"])
    op.create_index("ix_printer_alerts_opened_at", "printer_alerts", ["opened_at"])

    op.create_table(
        "endpoint_agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", name="fk_endpoint_agents_site_id"), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("agent_version", sa.String(length=50), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("pending_queue_size", sa.Integer(), nullable=True),
        sa.Column("failed_queue_size", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("token_hash", sa.String(length=128), nullable=True),
        sa.Column("token_created_at", sa.DateTime(), nullable=True),
        sa.Column("token_rotated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("uuid", name="uq_endpoint_agents_uuid"),
        sa.UniqueConstraint("site_id", "hostname", name="uq_endpoint_agents_site_hostname"),
    )
    op.create_index("ix_endpoint_agents_site_id", "endpoint_agents", ["site_id"])

    op.create_table(
        "forecast_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column("metric", sa.String(length=30), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=50), nullable=True),
        sa.Column("model_version", sa.String(length=20), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("history_days_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wape", sa.Float(), nullable=True),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("insufficient_history", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("forecast_json", sa.Text(), nullable=True),
        sa.UniqueConstraint("scope_type", "scope_id", "metric", "horizon_days", name="uq_forecast_run"),
    )
    op.create_index("ix_forecast_runs_scope_type", "forecast_runs", ["scope_type"])
    op.create_index("ix_forecast_runs_scope_id", "forecast_runs", ["scope_id"])

    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "endpoint_agent_id", sa.Integer(),
                sa.ForeignKey("endpoint_agents.id", name="fk_print_jobs_endpoint_agent_id"), nullable=True,
            )
        )
        batch_op.create_unique_constraint("uq_print_jobs_endpoint_record", ["endpoint_agent_id", "record_id"])
    op.create_index("ix_print_jobs_endpoint_agent_id", "print_jobs", ["endpoint_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_print_jobs_endpoint_agent_id", table_name="print_jobs")
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_constraint("uq_print_jobs_endpoint_record", type_="unique")
        batch_op.drop_column("endpoint_agent_id")

    op.drop_table("forecast_runs")

    op.drop_index("ix_endpoint_agents_site_id", table_name="endpoint_agents")
    op.drop_table("endpoint_agents")

    op.drop_table("printer_alerts")
    op.drop_table("printer_supply_samples")
    op.drop_table("printer_counter_samples")
    op.drop_table("printer_health_samples")

    op.drop_index("ix_monitoring_runs_source", table_name="monitoring_runs")
    op.drop_index("ix_monitoring_runs_site_id", table_name="monitoring_runs")
    op.drop_table("monitoring_runs")

    op.drop_table("printer_device_queue_links")

    op.drop_index("ix_printer_devices_print_server_id", table_name="printer_devices")
    op.drop_index("ix_printer_devices_site_id", table_name="printer_devices")
    op.drop_table("printer_devices")

    op.drop_table("snmp_profiles")
