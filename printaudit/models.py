from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from printaudit.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    cost_center_code = Column(String(50), nullable=True)

    users = relationship("User", back_populates="department")


class User(Base):
    __tablename__ = "users"

    user_name = Column(String(200), primary_key=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    department = relationship("Department", back_populates="users")


class PriceList(Base):
    __tablename__ = "price_list"

    id = Column(Integer, primary_key=True)
    # Glob-паттерн имени принтера/очереди, например "HP-Color-3F*" или "*" (fnmatch, регистр не важен)
    printer_name_pattern = Column(String(200), nullable=False)
    is_color = Column(Boolean, nullable=False, default=False)
    price_per_page = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="KZT")
    # Правила с большим priority проверяются первыми (конкретные паттерны должны быть выше "*")
    priority = Column(Integer, nullable=False, default=0)


class PrintJob(Base):
    __tablename__ = "print_jobs"

    id = Column(Integer, primary_key=True)
    site_code = Column(String(50), nullable=False, index=True)
    record_id = Column(Integer, nullable=False)  # EventRecordID из PrintService/Operational
    job_id = Column(String(50), nullable=True)  # Job Id из тела события (не уникален глобально)
    time_created = Column(DateTime, nullable=False, index=True)
    user_name = Column(String(200), nullable=False, index=True)
    document_name = Column(String(500), nullable=True)
    printer_name = Column(String(200), nullable=False, index=True)
    total_pages = Column(Integer, nullable=False, default=0)
    is_color = Column(Boolean, nullable=True)  # см. price_list — определяется по имени принтера/очереди
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True, index=True)
    price_per_page = Column(Float, nullable=True)
    cost = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    department = relationship("Department")

    __table_args__ = (
        UniqueConstraint("site_code", "record_id", name="uq_print_jobs_site_record"),
    )


class CollectorState(Base):
    """Курсор инкрементального чтения журнала событий — по одной строке на площадку (site_code)."""

    __tablename__ = "collector_state"

    site_code = Column(String(50), primary_key=True)
    last_record_id = Column(Integer, nullable=False, default=0)
    last_run_at = Column(DateTime, nullable=True)
