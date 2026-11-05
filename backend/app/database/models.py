from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import (
    JSON, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint, event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database.connection import Base

# The ONLY two employee-remark statuses. HR/AI outcomes live in hr_review_cases, never here.
PENDING_REMARK = "Pending Employee Remark"
REMARK_SUBMITTED = "Remark Submitted"
REMARK_STATUSES = (PENDING_REMARK, REMARK_SUBMITTED)
LATE_ARRIVAL = "LATE_ARRIVAL"
EARLY_DEPARTURE = "EARLY_DEPARTURE"


class UTCDateTime(TypeDecorator):
    """Stores timezone-aware datetimes as UTC; always returns aware UTC datetimes (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    employee_name: Mapped[str] = mapped_column(String(200), nullable=False)
    verified_email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)  # stored lower-case
    department: Mapped[str | None] = mapped_column(String(100))
    designation: Mapped[str | None] = mapped_column(String(100))
    location: Mapped[str | None] = mapped_column(String(100))
    assigned_shift: Mapped[str | None] = mapped_column(String(100))  # name in shift_master (HR-maintained)
    account_status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="EMPLOYEE")
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True)  # bound at first registration
    registered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("account_status IN ('ACTIVE','INACTIVE','PENDING_APPROVAL')", name="ck_emp_status"),
        CheckConstraint("role IN ('EMPLOYEE')", name="ck_emp_role"),
        CheckConstraint("verified_email = lower(verified_email)", name="ck_emp_email_lower"),
        Index("ix_emp_dept", "department"),
    )


class ShiftMaster(Base):
    """HR-maintained (via the Shift Master sheet). No shifts are built into the code."""

    __tablename__ = "shift_master"
    shift_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)  # end <= start means overnight
    late_tolerance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    early_tolerance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("late_tolerance_minutes BETWEEN 0 AND 240", name="ck_shift_late"),
        CheckConstraint("early_tolerance_minutes BETWEEN 0 AND 240", name="ck_shift_early"),
        CheckConstraint("start_time <> end_time", name="ck_shift_distinct"),
    )


class AttendanceEvent(Base):
    """Append-only (enforced by ORM guard + database triggers). Never updated or deleted."""

    __tablename__ = "attendance_events"
    attendance_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(3), nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="QR")
    network_verified: Mapped[bool] = mapped_column(nullable=False, default=False)  # legacy: no network rule any more (always False)
    # LEGACY / unused: the WFO-WFH-WFCO work mode was removed. The column stays (append-only table) with its default.
    work_mode: Mapped[str] = mapped_column(String(4), nullable=False, default="WFO", server_default="WFO")
    client_ip: Mapped[str | None] = mapped_column(String(45))
    recorded_by: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    request_key: Mapped[str | None] = mapped_column(String(64))  # client idempotency key
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    employee: Mapped[Employee] = relationship()

    __table_args__ = (
        CheckConstraint("event_type IN ('IN','OUT')", name="ck_evt_type"),
        CheckConstraint("source IN ('QR','ADMIN_ASSISTED','WFH')", name="ck_evt_source"),
        CheckConstraint("work_mode IN ('WFO','WFH','WFCO')", name="ck_evt_mode"),
        UniqueConstraint("employee_id", "request_key", name="uq_evt_request_key"),
        Index("ix_evt_emp_ts", "employee_id", "event_timestamp"),
        Index("ix_evt_ts", "event_timestamp"),
    )


@event.listens_for(AttendanceEvent, "before_update")
@event.listens_for(AttendanceEvent, "before_delete")
def _events_are_append_only(mapper, connection, target):
    raise RuntimeError("attendance_events is append-only")


APPEND_ONLY_SQLITE = [
    "CREATE TRIGGER IF NOT EXISTS trg_events_no_update BEFORE UPDATE ON attendance_events "
    "BEGIN SELECT RAISE(ABORT, 'attendance_events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_events_no_delete BEFORE DELETE ON attendance_events "
    "BEGIN SELECT RAISE(ABORT, 'attendance_events is append-only'); END",
]
APPEND_ONLY_PG = [
    "CREATE OR REPLACE FUNCTION attendance_events_immutable() RETURNS trigger AS $$ BEGIN "
    "RAISE EXCEPTION 'attendance_events is append-only'; END; $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_events_immutable ON attendance_events",
    "CREATE TRIGGER trg_events_immutable BEFORE UPDATE OR DELETE ON attendance_events "
    "FOR EACH ROW EXECUTE FUNCTION attendance_events_immutable()",
]


def install_append_only_triggers(conn) -> None:
    stmts = APPEND_ONLY_PG if conn.dialect.name == "postgresql" else APPEND_ONLY_SQLITE
    for s in stmts:
        conn.execute(text(s))


class AttendanceRemark(Base):
    """Employee explanation for a late arrival / early departure. Separate from the raw event."""

    __tablename__ = "attendance_remarks"
    remark_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    attendance_id: Mapped[int] = mapped_column(ForeignKey("attendance_events.attendance_id"), nullable=False)
    exception_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_category: Mapped[str | None] = mapped_column(String(100))
    employee_remark: Mapped[str | None] = mapped_column(Text)
    remark_status: Mapped[str] = mapped_column(String(30), nullable=False, default=PENDING_REMARK)
    shift_name: Mapped[str] = mapped_column(String(100), nullable=False)
    scheduled_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    variance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    employee: Mapped[Employee] = relationship()
    event: Mapped[AttendanceEvent] = relationship()

    __table_args__ = (
        CheckConstraint("remark_status IN ('Pending Employee Remark','Remark Submitted')", name="ck_remark_status"),
        CheckConstraint("exception_type IN ('LATE_ARRIVAL','EARLY_DEPARTURE')", name="ck_remark_type"),
        CheckConstraint(
            "(remark_status = 'Pending Employee Remark' AND submitted_at IS NULL) OR "
            "(remark_status = 'Remark Submitted' AND submitted_at IS NOT NULL AND reason_category IS NOT NULL)",
            name="ck_remark_consistent"),
        UniqueConstraint("attendance_id", "exception_type", name="uq_remark_event_type"),
        Index("ix_remark_emp", "employee_id", "created_at"),
        Index("ix_remark_status", "remark_status"),
    )


class AdminUser(Base):
    __tablename__ = "admin_users"
    admin_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="HR_ADMIN")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        CheckConstraint("role IN ('SUPER_ADMIN','HR_ADMIN','AUDITOR')", name="ck_admin_role"),
        CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_admin_status"),
        CheckConstraint("email = lower(email)", name="ck_admin_email_lower"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    meta: Mapped[dict | None] = mapped_column("metadata", JSON)

    __table_args__ = (Index("ix_audit_ts", "timestamp"), Index("ix_audit_actor", "actor"),
                      Index("ix_audit_action", "action"))
