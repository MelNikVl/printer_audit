"""snmp_profiles: real SNMPv3 USM fields (username/auth/priv)

Ранняя реализация direct_snmp опрашивала устройство ВСЕГДА через
SNMPv2c/CommunityData, независимо от snmp_profiles.snmp_version="v3" по
умолчанию — сам протокол USM (SNMPv3: имя пользователя, протокол/ключ
аутентификации, протокол/ключ приватности) никогда не был реализован, а
credentials_env_var трактовался как community-строка для ЛЮБОЙ версии.
См. printaudit/monitoring/snmp_adapter.py::resolve_snmp_security за полной
реализацией.

Добавляет 5 nullable-колонок для snmp_version="v3":
  - snmp_v3_username -- имя пользователя USM (не секрет, хранится как есть)
  - snmp_v3_auth_protocol -- "SHA"/"MD5"/"SHA224"/"SHA256"/"SHA384"/"SHA512"
    или NULL (noAuth)
  - snmp_v3_auth_key_env_var -- ИМЯ переменной окружения с auth passphrase
    (сам секрет никогда не хранится в БД)
  - snmp_v3_priv_protocol -- "AES"/"AES192"/"AES256"/"DES"/"3DES" или NULL
    (noPriv)
  - snmp_v3_priv_key_env_var -- ИМЯ переменной окружения с priv passphrase

credentials_env_var (существующая колонка) теперь используется ТОЛЬКО при
snmp_version="v2c" (явный legacy-режим) — не переименована и не удалена,
чтобы миграция оставалась чисто аддитивной; смысл сужен только в docstring
модели (printaudit/models.py::SnmpProfile) и адаптере, без изменения схемы
для v2c-профилей.

Полностью аддитивно: существующие строки snmp_profiles получают все 5
новых полей NULL (что означает noAuthNoPriv для v3-профилей, требующих
дозаполнения администратором до реального опроса — resolve_snmp_security
явно откажет с понятной ошибкой, если snmp_v3_username не задан).

Revision ID: 7c9d3e1a5b02
Revises: cca38199a688
Create Date: 2026-09-05 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '7c9d3e1a5b02'
down_revision: Union[str, None] = 'cca38199a688'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("snmp_profiles") as batch_op:
        batch_op.add_column(sa.Column("snmp_v3_username", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("snmp_v3_auth_protocol", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("snmp_v3_auth_key_env_var", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("snmp_v3_priv_protocol", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("snmp_v3_priv_key_env_var", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("snmp_profiles") as batch_op:
        batch_op.drop_column("snmp_v3_priv_key_env_var")
        batch_op.drop_column("snmp_v3_priv_protocol")
        batch_op.drop_column("snmp_v3_auth_key_env_var")
        batch_op.drop_column("snmp_v3_auth_protocol")
        batch_op.drop_column("snmp_v3_username")
