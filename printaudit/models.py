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
    total_pages = Column(Integer, nullable=False, default=0)
    is_color = Column(Boolean, nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True, index=True)
    printer_queue_id = Column(Integer, ForeignKey("printer_queues.id"), nullable=True, index=True)
    # Тариф и цена ЗАФИКСИРОВАНЫ на момент вставки и больше не пересчитываются
    # при изменении price_rules/price_list — price_rule_id — это только ссылка
    # для трассировки "почему так посчитано", а не источник истины для отчётов.
    price_rule_id = Column(Integer, ForeignKey("price_rules.id"), nullable=True)
    price_per_page = Column(Float, nullable=True)
    cost = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    department = relationship("Department")
    printer_queue = relationship("PrinterQueue")

    __table_args__ = (
        UniqueConstraint("site_code", "record_id", name="uq_print_jobs_site_record"),
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
    __tablename__ = "printer_queues"

    id = Column(Integer, primary_key=True)
    printer_name = Column(String(200), unique=True, nullable=False, index=True)
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
