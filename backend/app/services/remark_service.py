"""Late-arrival / early-departure detection (Python only) and employee remarks.

remark_status has exactly two values (see models). Only the employee's own submission moves
Pending -> Submitted; nothing else (including the LLM) can change it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import (
    AttendanceEvent, AttendanceRemark, Employee, EARLY_DEPARTURE, LATE_ARRIVAL, PENDING_REMARK, REMARK_SUBMITTED,
)
from app.services import attendance_service as att
from app.services import shift_service as sh
from app.services.audit import audit

REASONS = {
    LATE_ARRIVAL: ["Traffic / Transportation delay", "Personal emergency", "Health-related reason",
                   "Work-related reason", "Other"],
    EARLY_DEPARTURE: ["Personal emergency", "Health-related reason", "Manager-approved early departure",
                      "Work-related reason", "Other"],
}


class RemarkError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def evaluate_punch(db: Session, settings: Settings, emp: Employee, ev: AttendanceEvent) -> AttendanceRemark | None:
    """Called inside the punch transaction (event already flushed). Creates a 'Pending Employee Remark' row
    when the punch is outside the shift tolerance. No valid shift -> no remark (and the issue is audited)."""
    shifts = sh.load_shifts(db)
    shift, issue = sh.resolve_shift(emp, shifts)
    if shift is None:
        audit(db, "system", "SHIFT_CONFIG_ISSUE", "employee", emp.employee_id, {"issue": issue})
        return None
    tz = settings.tz
    if ev.event_type == "IN":
        wd = ev.event_timestamp.astimezone(tz).date()
        start, end = att.day_bounds(wd, settings)
        earlier_in = db.scalar(select(AttendanceEvent.attendance_id).where(
            AttendanceEvent.employee_id == emp.id, AttendanceEvent.event_type == "IN",
            AttendanceEvent.event_timestamp >= start, AttendanceEvent.event_timestamp < ev.event_timestamp,
            AttendanceEvent.attendance_id != ev.attendance_id))
        if earlier_in is not None:
            return None  # only the first IN of the day can be a "late arrival"
        delay = sh.late_delay_minutes(shift, ev.event_timestamp, wd, tz)
        if delay <= shift.late_tol:
            return None
        kind, variance, sched = LATE_ARRIVAL, delay, sh.scheduled_start(shift, wd, tz)
    else:
        prev_in = db.scalar(select(AttendanceEvent).where(
            AttendanceEvent.employee_id == emp.id, AttendanceEvent.event_type == "IN",
            AttendanceEvent.event_timestamp < ev.event_timestamp).order_by(AttendanceEvent.event_timestamp.desc()))
        if prev_in is None:
            return None
        wd = prev_in.event_timestamp.astimezone(tz).date()
        early = sh.early_departure_minutes(shift, ev.event_timestamp, wd, tz)
        if early <= shift.early_tol:
            return None
        kind, variance, sched = EARLY_DEPARTURE, early, sh.scheduled_end(shift, wd, tz)
    r = AttendanceRemark(employee_id=emp.id, attendance_id=ev.attendance_id, exception_type=kind,
                         remark_status=PENDING_REMARK, shift_name=shift.name, scheduled_time=sched,
                         variance_minutes=variance)
    db.add(r)
    db.flush()
    audit(db, "system", "REMARK_REQUESTED", "remark", r.remark_id,
          {"employee": emp.employee_id, "type": kind, "variance_minutes": variance, "shift": shift.name})
    return r


def remark_view(r: AttendanceRemark, settings: Settings) -> dict:
    actual = r.event.event_timestamp
    a, s = att.fmt_time(actual, settings), att.fmt_time(r.scheduled_time, settings)
    if r.exception_type == LATE_ARRIVAL:
        title = "Late Arrival"
        msg = f"You punched in at {a}. Your scheduled shift starts at {s}."
        ask = "Please provide a reason for your late arrival."
        detail = f"Delay: {sh.fmt_minutes(r.variance_minutes)}."
    else:
        title = "Early Punch Out"
        msg = f"You punched out at {a}. Your scheduled shift ends at {s}."
        ask = "Please provide a reason for your early departure."
        detail = f"Early departure: {sh.fmt_minutes(r.variance_minutes)}."
    return {"remark_id": r.remark_id, "exception_type": r.exception_type, "title": title, "message": msg,
            "detail": detail, "prompt": ask, "reasons": REASONS[r.exception_type], "shift": r.shift_name,
            "work_date": r.scheduled_time.astimezone(settings.tz).date().isoformat(),
            "actual_time": a, "scheduled_time": s, "variance_minutes": r.variance_minutes,
            "remark_status": r.remark_status, "reason_category": r.reason_category,
            "employee_remark": r.employee_remark,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None}


def list_own(db: Session, settings: Settings, emp: Employee, pending_only: bool = False) -> list[dict]:
    q = select(AttendanceRemark).where(AttendanceRemark.employee_id == emp.id)
    if pending_only:
        q = q.where(AttendanceRemark.remark_status == PENDING_REMARK)
    return [remark_view(r, settings) for r in db.scalars(q.order_by(AttendanceRemark.remark_id.desc()).limit(200))]


def submit_remark(db: Session, settings: Settings, emp: Employee, remark_id: int, category: str,
                  text: str | None) -> AttendanceRemark:
    r = db.get(AttendanceRemark, remark_id)
    if r is None or r.employee_id != emp.id:  # same answer for "not yours" and "not found"
        raise RemarkError("not_found", "Remark request not found", 404)
    if r.remark_status != PENDING_REMARK:
        raise RemarkError("already_submitted", "This remark was already submitted and cannot be changed.", 409)
    if category not in REASONS[r.exception_type]:
        raise RemarkError("bad_reason", "Please choose one of the listed reasons.", 422)
    text = (text or "").strip()
    if len(text) > 1000:
        raise RemarkError("too_long", "Remarks are limited to 1000 characters.", 422)
    r.reason_category, r.employee_remark = category, text or None
    r.remark_status, r.submitted_at = REMARK_SUBMITTED, att.now_utc()
    audit(db, emp.verified_email, "REMARK_SUBMITTED", "remark", r.remark_id,
          {"type": r.exception_type, "category": category, "has_text": bool(text)})
    db.commit()
    return r
