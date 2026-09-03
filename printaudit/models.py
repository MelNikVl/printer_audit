import uuid as _uuid_module
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from printaudit.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(_uuid_module.uuid4())


# ---------------------------------------------------------------------------
# Существующие таблицы MVP (сохранены как есть; новые nullable-колонки —
# расширение, не breaking change для уже накопленных данных).
# ---------------------------------------------------------------------------


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    cost_center_code = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=0)

    users = relationship("User", back_populates="department")


class User(Base):
    """Легаси-таблица ручного CSV-маппинга (users_departments.csv). Остаётся
    рабочим fallback-путём там, где AD не подключён; при наличии совпадающего
    AdUser с назначенным отделом приоритет у AdUser (см. printaudit.department_resolver)."""

    __tablename__ = "users"

    user_name = Column(String(200), primary_key=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    department = relationship("Department", back_populates="users")


class PriceList(Base):
    """Легаси-таблица простого тарифа по маске принтера. Сохранена для
    обратной совместимости; новые тарифы задаются через PriceRule, который
    поддерживает период действия, приоритет и историчность."""

    __tablename__ = "price_list"

    id = Column(Integer, primary_key=True)
    printer_name_pattern = Column(String(200), nullable=False)
    is_color = Column(Boolean, nullable=False, default=False)
    price_per_page = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="KZT")
    priority = Column(Integer, nullable=False, default=0)


class PrintJob(Base):
    """Одно напечатанное задание. `site_code` — исторический денормализованный
    код площадки, оставлен как есть для обратной совместимости; `site_id`/
    `print_server_id` — новые ссылки на Site/PrintServer (см. ниже), которые
    делают идемпотентность и отчётность корректными в многоплощадочной/
    централизованной установке (см. docs/MULTISITE_ARCHITECTURE.md).

    ВАЖНО про уникальность: старое ограничение (site_code, record_id) снято
    миграцией 80b73be83524 — EventRecordID уникален только в пределах ОДНОГО
    журнала событий ОДНОГО Windows Print Server, а не в пределах площадки.
    Если на площадке два Print Server, у каждого свой независимый счётчик
    RecordId, и оба могут прислать record_id=42; со старым ограничением
    второй Print Server не смог бы записать своё задание. Настоящий (и
    единственный) ключ идемпотентности теперь — (print_server_id, record_id),
    см. uq_print_jobs_server_record. print_server_id проставляется
    автоматически (в standalone/agent-режиме коллектор сам заводит себе
    "неявный" PrintServer при первом запуске — см.
    printaudit.sites.get_or_create_print_server); остаётся nullable на уровне
    схемы только для редкого случая, когда историческая БД содержала записи
    более чем одной площадки и миграция не смогла однозначно сопоставить
    старые строки серверу (см. docs/MULTISITE_ARCHITECTURE.md)."""

    __tablename__ = "print_jobs"

    id = Column(Integer, primary_key=True)
    site_code = Column(String(50), nullable=False, index=True)
    record_id = Column(Integer, nullable=False)
    job_id = Column(String(50), nullable=True)
    time_created = Column(DateTime, nullable=False, index=True)
    user_name = Column(String(200), nullable=False, index=True)
    # Нормализованный логин (нижний регистр, без учёта DOMAIN\ / @domain) —
    # для регистронезависимого и формато-независимого сопоставления с AdUser.
    # Заполняется коллектором через printaudit.ad_normalize.normalize_login().
    user_login_normalized = Column(String(200), nullable=True, index=True)
    document_name = Column(String(500), nullable=True)
    printer_name = Column(String(200), nullable=False, index=True)
    # Компьютер, с которого пришло задание (если Event 307 несёт эту информацию
    # на конкретном сервере/драйвере — не гарантировано, см. docs/RESEARCH.md).
    source_computer = Column(String(200), nullable=True)
    total_pages = Column(Integer, nullable=False, default=0)
    # copies/pages_per_copy заполняются ТОЛЬКО если коллектор надёжно извлёк их
    # из события (никаких выдуманных значений для старых/непроверенных
    # серверов) — см. docs/MULTISITE_ARCHITECTURE.md, раздел про total_pages.
    copies = Column(Integer, nullable=True)
    pages_per_copy = Column(Integer, nullable=True)
    is_color = Column(Boolean, nullable=True)
    # event | queue | unknown — откуда взято значение is_color, см.
    # printaudit.printers.resolver.resolve_price. unknown НЕ означает Ч/Б.
    color_source = Column(String(10), nullable=False, default="unknown")
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True, index=True)
    printer_queue_id = Column(Integer, ForeignKey("printer_queues.id"), nullable=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=True, index=True)
    print_server_id = Column(Integer, ForeignKey("print_servers.id"), nullable=True, index=True)
    # Тариф и цена ЗАФИКСИРОВАНЫ на момент вставки и больше не пересчитываются
    # при изменении price_rules/price_list — price_rule_id — это только ссылка
    # для трассировки "почему так посчитано", а не источник истины для отчётов.
    price_rule_id = Column(Integer, ForeignKey("price_rules.id"), nullable=True)
    price_per_page = Column(Float, nullable=True)
    cost = Column(Float, nullable=True)
    currency = Column(String(10), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    department = relationship("Department")
    printer_queue = relationship("PrinterQueue")
    site = relationship("Site")
    print_server = relationship("PrintServer")

    __table_args__ = (
        UniqueConstraint("print_server_id", "record_id", name="uq_print_jobs_server_record"),
    )


class CollectorState(Base):
    """Курсор инкрементального чтения журнала событий — по одной строке на площадку (site_code)."""

    __tablename__ = "collector_state"

    site_code = Column(String(50), primary_key=True)
    last_record_id = Column(Integer, nullable=False, default=0)
    last_run_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Приложение: локальные учётные записи администраторов/просматривающих
# ---------------------------------------------------------------------------


class AppUser(Base):
    """Локальная учётка приложения — либо привязанная к личности в AD (вход
    прямым bind-ом к AD на каждый логин, пароль никогда не хранится), либо
    полностью локальная (auth_provider="local", пароль — только как
    Argon2id-хэш). См. printaudit.security.local_auth / printaudit.ad.client."""

    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True)
    ad_sid = Column(String(200), unique=True, nullable=True)
    ad_object_guid = Column(String(64), unique=True, nullable=True)
    # DOMAIN\sam (AD) или просто логин в нижнем регистре (local) —
    # см. printaudit.ad_normalize.normalize_login
    login_normalized = Column(String(200), unique=True, nullable=False, index=True)
    display_name = Column(String(300), nullable=True)
    email = Column(String(300), nullable=True)
    role = Column(String(20), nullable=False)  # superadmin | admin | viewer
    is_active = Column(Boolean, nullable=False, default=True)

    # "local" | "ad" — какой провайдер проверяет вход для этой учётки.
    auth_provider = Column(String(10), nullable=False, default="ad")
    # Argon2id-хэш (см. printaudit.security.passwords) — NULL для auth_provider="ad".
    password_hash = Column(String(300), nullable=True)
    # True сразу после создания админом/bootstrap с временным паролем —
    # require_login принудительно перенаправляет на /change-password, пока не False.
    must_change_password = Column(Boolean, nullable=False, default=False)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    password_changed_at = Column(DateTime, nullable=True)

    assigned_by_id = Column(Integer, ForeignKey("app_users.id"), nullable=True)
    assigned_at = Column(DateTime, nullable=False, default=_utcnow)
    disabled_at = Column(DateTime, nullable=True)
    disabled_by_id = Column(Integer, ForeignKey("app_users.id"), nullable=True)

    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class WebSession(Base):
    """Серверная сессия. `id` — это SHA-256 (с секретом приложения) от
    случайного токена сессии, который лежит в HttpOnly cookie у клиента: сам
    "сырой" токен в БД не хранится, поэтому утечка БД не даёт немедленно
    угнать активные сессии."""

    __tablename__ = "web_sessions"

    id = Column(String(64), primary_key=True)  # hex sha256
    app_user_id = Column(Integer, ForeignKey("app_users.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    last_seen_at = Column(DateTime, nullable=False, default=_utcnow)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(300), nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    app_user = relationship("AppUser")


# ---------------------------------------------------------------------------
# Кэш и правила Active Directory
# ---------------------------------------------------------------------------


class AdUser(Base):
    """Локальный кэш карточек пользователей AD, наполняется поиском/импортом
    через printaudit.ad.client. Используется и для UI администрирования, и
    как справочник для сопоставления заданий печати с отделом."""

    __tablename__ = "ad_users"

    id = Column(Integer, primary_key=True)
    sid = Column(String(200), unique=True, nullable=True)
    object_guid = Column(String(64), unique=True, nullable=True)
    sam_account_name = Column(String(200), nullable=False)
    domain = Column(String(100), nullable=True)
    login_normalized = Column(String(200), unique=True, nullable=False, index=True)
    display_name = Column(String(300), nullable=True)
    email = Column(String(300), nullable=True)

    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True, index=True)
    # manual | group_rule | none — откуда взят текущий department_id
    department_source = Column(String(20), nullable=False, default="none")
    # True = отдел выставлен вручную и AD-синхронизация НЕ имеет права его менять
    department_locked = Column(Boolean, nullable=False, default=False)
    department_rule_id = Column(Integer, ForeignKey("ad_department_rules.id"), nullable=True)

    is_ad_enabled = Column(Boolean, nullable=False, default=True)
    local_disabled = Column(Boolean, nullable=False, default=False)

    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    department = relationship("Department", foreign_keys=[department_id])


class AdGroup(Base):
    __tablename__ = "ad_groups"

    id = Column(Integer, primary_key=True)
    dn = Column(String(500), unique=True, nullable=False)
    sam_account_name = Column(String(200), nullable=True)
    display_name = Column(String(300), nullable=True)
    description = Column(Text, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class AdGroupMembership(Base):
    __tablename__ = "ad_group_memberships"

    id = Column(Integer, primary_key=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id"), nullable=False, index=True)
    ad_user_id = Column(Integer, ForeignKey("ad_users.id"), nullable=False, index=True)
    synced_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("ad_group_id", "ad_user_id", name="uq_ad_group_membership"),
    )


class AdDepartmentRule(Base):
    """Правило "AD-группа -> отдел". Один активный набор правил на группу;
    приоритет разрешает конфликт, если пользователь состоит в нескольких
    сопоставленных группах (см. printaudit.ad_rules.resolve_department_for_user)."""

    __tablename__ = "ad_department_rules"

    id = Column(Integer, primary_key=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id"), unique=True, nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    priority = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by_id = Column(Integer, ForeignKey("app_users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    ad_group = relationship("AdGroup")
    department = relationship("Department")


# ---------------------------------------------------------------------------
# Принтеры/очереди и версионированные тарифы
# ---------------------------------------------------------------------------


class PrinterQueue(Base):
    """Одна очередь печати. `printer_name` больше НЕ уникально глобально —
    одинаковое имя очереди легко повторяется на разных площадках/серверах
    (например, "HP-3F-BW" на двух объектах). Уникальность теперь —
    (print_server_id, printer_name), см. uq_printer_queues_server_name;
    print_server_id nullable по той же причине, что и в PrintJob (см. её
    docstring и docs/MULTISITE_ARCHITECTURE.md)."""

    __tablename__ = "printer_queues"

    id = Column(Integer, primary_key=True)
    printer_name = Column(String(200), nullable=False, index=True)
    print_server_id = Column(Integer, ForeignKey("print_servers.id"), nullable=True, index=True)
    display_name = Column(String(200), nullable=True)
    server_name = Column(String(200), nullable=True)
    share_name = Column(String(200), nullable=True)
    driver_name = Column(String(300), nullable=True)
    port_name = Column(String(200), nullable=True)
    location = Column(String(300), nullable=True)
    comment = Column(String(500), nullable=True)
    is_shared = Column(Boolean, nullable=False, default=False)
    is_published = Column(Boolean, nullable=False, default=False)
    printer_status = Column(String(50), nullable=True)

    # unknown | bw | color — см. docs/ADMIN_GUIDE.md про раздельные очереди
    color_mode = Column(String(10), nullable=False, default="unknown")
    collection_enabled = Column(Boolean, nullable=False, default=True)
    # Быстрый тариф без периода действия/приоритета (для простых кейсов);
    # полноценные правила — в price_rules.
    price_per_page = Column(Float, nullable=True)
    currency = Column(String(10), nullable=False, default="KZT")

    first_seen_at = Column(DateTime, nullable=False, default=_utcnow)
    last_seen_at = Column(DateTime, nullable=True)
    last_job_at = Column(DateTime, nullable=True)
    # Присутствует в последней синхронизации Get-Printer. Исчезновение очереди
    # НИКОГДА не удаляет саму очередь/её историю — только снимает этот флаг.
    is_active = Column(Boolean, nullable=False, default=True)
    # True, если запись создана автоматически коллектором при первой печати
    # через ещё не засинхронизированную очередь (см. printaudit.printers.resolver)
    discovered_by_collector = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    print_server = relationship("PrintServer")

    __table_args__ = (
        UniqueConstraint("print_server_id", "printer_name", name="uq_printer_queues_server_name"),
    )


class PriceRule(Base):
    """Версионированный тариф. printer_queue_id=NULL — правило "по умолчанию"
    для всех очередей без более конкретного правила. Историческая стоимость
    уже вставленных print_jobs НЕ пересчитывается при изменении/добавлении
    правил — см. PrintJob.price_rule_id/price_per_page/cost."""

    __tablename__ = "price_rules"

    id = Column(Integer, primary_key=True)
    printer_queue_id = Column(Integer, ForeignKey("printer_queues.id"), nullable=True, index=True)
    is_color = Column(Boolean, nullable=False, default=False)
    price_per_page = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="KZT")
    valid_from = Column(DateTime, nullable=True)
    valid_to = Column(DateTime, nullable=True)
    priority = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by_id = Column(Integer, ForeignKey("app_users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    printer_queue = relationship("PrinterQueue")


# ---------------------------------------------------------------------------
# Аудит и журналы синхронизаций
# ---------------------------------------------------------------------------


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    actor_app_user_id = Column(Integer, ForeignKey("app_users.id"), nullable=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    object_type = Column(String(100), nullable=False, index=True)
    object_id = Column(String(100), nullable=True)
    old_value = Column(Text, nullable=True)  # JSON-строка, без секретов (см. printaudit.audit)
    new_value = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    session_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow, index=True)

    actor = relationship("AppUser")


class SyncRun(Base):
    """История запусков коллектора / AD-синхронизации / обнаружения принтеров.
    Расширяет collector_state (который хранит только текущий курсор) историей
    по каждому запуску — это то, что показывается на странице /admin (Обзор)."""

    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True)
    run_type = Column(String(30), nullable=False, index=True)  # collector | ad_sync | printer_discovery
    site_code = Column(String(50), nullable=True, index=True)
    started_at = Column(DateTime, nullable=False, default=_utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running")  # running | success | failed
    events_fetched = Column(Integer, nullable=False, default=0)
    inserted = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    duplicates = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    details_json = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Multisite: площадки, Print Server/агенты, исходящая очередь доставки
# ---------------------------------------------------------------------------


class Site(Base):
    """Физическая площадка (объект). `site_code` — тот же смысл, что и
    исторический printaudit.config.Settings.site_code, но теперь как
    отдельная сущность с устойчивым uuid — так центральный сервер может
    ссылаться на площадку, не завязываясь на локальный integer id (см.
    docs/MULTISITE_ARCHITECTURE.md)."""

    __tablename__ = "sites"

    id = Column(Integer, primary_key=True)
    uuid = Column(String(36), unique=True, nullable=False, default=_new_uuid)
    site_code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class PrintServer(Base):
    """Регистрация одного Windows Print Server / агента на площадке.
    `status` НЕ хранится как поле — вычисляется по возрасту last_heartbeat_at
    (см. printaudit.sites.compute_status), чтобы не оказаться навсегда
    "застрявшим" значением, если агент просто перестал слать heartbeat.

    `token_hash` — SHA-256 сырого токена агента (см. printaudit.security.agent_tokens);
    сам токен НИКОГДА не хранится и показывается администратору только один
    раз, в момент создания регистрации/ротации (см. webapp/print_servers_routes.py).

    Про last_contact_at/last_sync_at/last_ingest_error (см. webapp/agent_api.py):
      - last_contact_at обновляется на КАЖДЫЙ успешно аутентифицированный
        запрос (batch ИЛИ heartbeat) — "агент достучался и токен верный".
      - last_sync_at обновляется ТОЛЬКО когда весь пакет /events/batch
        обработан без единого rejected события (inserted/duplicate — оба
        считаются успехом). Если хотя бы одно событие отклонено, last_sync_at
        не трогается, а причина — в last_ingest_error.
      - last_error — то, что САМ агент сообщил о себе через heartbeat
        (например, ошибка отправки на его стороне); last_ingest_error —
        то, что ЦЕНТР обнаружил при разборе присланных данных. Разные поля
        осознанно, чтобы одно не затирало другое."""

    __tablename__ = "print_servers"

    id = Column(Integer, primary_key=True)
    uuid = Column(String(36), unique=True, nullable=False, default=_new_uuid)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False, index=True)
    server_name = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=True)
    agent_version = Column(String(50), nullable=True)
    protocol_version = Column(Integer, nullable=True)
    last_heartbeat_at = Column(DateTime, nullable=True)
    last_contact_at = Column(DateTime, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    pending_queue_size = Column(Integer, nullable=True)
    # Только терминально отклонённые (не сетевые сбои) — см. OutboxEvent.status.
    failed_queue_size = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    last_ingest_error = Column(Text, nullable=True)
    is_disabled = Column(Boolean, nullable=False, default=False)
    token_hash = Column(String(128), nullable=True)
    token_created_at = Column(DateTime, nullable=True)
    token_rotated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    site = relationship("Site")

    __table_args__ = (
        UniqueConstraint("site_id", "server_name", name="uq_print_servers_site_server"),
    )


class OutboxEvent(Base):
    """Durable outbox для агента: одна строка на каждое задание печати,
    ожидающее (или уже получившее подтверждение) отправки в центр. Заводится
    В ТОЙ ЖЕ транзакции, что и сам PrintJob (см. collector/collect_print_events.py),
    поэтому перезапуск агента между вставкой задания и его отправкой не может
    "потерять" задание — см. docs/MULTISITE_ARCHITECTURE.md, раздел про
    гарантии доставки."""

    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True)
    print_job_id = Column(Integer, ForeignKey("print_jobs.id"), nullable=False, unique=True, index=True)
    # pending | delivered | failed — "failed" здесь означает "центр явно
    # отверг событие как невалидное", не "сеть недоступна" (в этом случае
    # строка остаётся pending и просто ждёт следующей попытки с backoff).
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    last_batch_id = Column(String(36), nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    print_job = relationship("PrintJob")
