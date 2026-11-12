"""Deterministic attendance logic. The database is the source of truth; nothing here uses an LLM."""
from __future__ import annotations

import calendar
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import (
    AttendanceEvent, AttendanceRemark, Employee, LATE_ARRIVAL, EARLY_DEPARTURE, PENDING_REMARK,
)
from app.services import shift_service as sh
from app.services.audit import audit

_sqlite_lock = threading.Lock()  # SQLite has no row locks; serialise punches in-process (dev only)


def now_utc() -> datetime:
    """Single clock for the whole backend (patched in tests). Always the SERVER clock."""
    return datetime.now(UTC)


class PunchError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


# ---------- session building ----------
@dataclass
class Session_:
    in_ts: datetime | None
    out_ts: datetime | None
    work_date: date
    in_event: AttendanceEvent | None = None
    out_event: AttendanceEvent | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.in_ts is not None and self.out_ts is None

    @property
    def seconds(self) -> int:
        if self.in_ts and self.out_ts:
            return max(0, int((self.out_ts - self.in_ts).total_seconds()))
        return 0


def local_date(ts: datetime, settings: Settings) -> date:
    return ts.astimezone(settings.tz).date()


def build_sessions(events: list[AttendanceEvent], settings: Settings) -> list[Session_]:
    """Pair chronologically ordered events into IN/OUT sessions. An OUT belongs to the work date of its
    IN, which handles overnight shifts. Orphans are flagged, never dropped or invented."""
    out: list[Session_] = []
    cur: Session_ | None = None
    for e in sorted(events, key=lambda x: (x.event_timestamp, x.attendance_id or 0)):
        if e.event_type == "IN":
            if cur is not None:
                cur.flags.append("MISSING_OUT")
                out.append(cur)
            cur = Session_(e.event_timestamp, None, local_date(e.event_timestamp, settings), in_event=e)
        else:
            if cur is None:
                out.append(Session_(None, e.event_timestamp, local_date(e.event_timestamp, settings),
                                    out_event=e, flags=["MISSING_IN"]))
            else:
                cur.out_ts, cur.out_event = e.event_timestamp, e
                out.append(cur)
                cur = None
    if cur is not None:
        out.append(cur)
    return out


def _events(db: Session, emp_pk: int | None, start_utc: datetime, end_utc: datetime) -> list[AttendanceEvent]:
    q = select(AttendanceEvent).where(AttendanceEvent.event_timestamp >= start_utc,
                                      AttendanceEvent.event_timestamp < end_utc)
    if emp_pk is not None:
        q = q.where(AttendanceEvent.employee_id == emp_pk)
    return list(db.scalars(q.order_by(AttendanceEvent.event_timestamp, AttendanceEvent.attendance_id)))


def day_bounds(d: date, settings: Settings) -> tuple[datetime, datetime]:
    s = datetime.combine(d, time.min, tzinfo=settings.tz)
    return s.astimezone(UTC), (s + timedelta(days=1)).astimezone(UTC)


def sessions_for_range(db: Session, settings: Settings, d1: date, d2: date,
                       emp_pk: int | None = None) -> dict[int, list[Session_]]:
    """Sessions grouped by employee whose work_date is in [d1, d2]. Reads a margin so an overnight
    session and its OUT are always paired correctly."""
    s, _ = day_bounds(d1 - timedelta(days=1), settings)
    _, e = day_bounds(d2 + timedelta(days=2), settings)
    by_emp: dict[int, list[AttendanceEvent]] = {}
    for ev in _events(db, emp_pk, s, e):
        by_emp.setdefault(ev.employee_id, []).append(ev)
    res: dict[int, list[Session_]] = {}
    for pk, evs in by_emp.items():
        ss = [x for x in build_sessions(evs, settings) if d1 <= x.work_date <= d2]
        if ss:
            res[pk] = ss
    return res


def fmt_time(ts: datetime | None, settings: Settings) -> str | None:
    return ts.astimezone(settings.tz).strftime("%I:%M %p") if ts else None


def summarise_day(sessions: list[Session_]) -> dict:
    """Wide-report summary policy: first IN of the work date, last OUT of the work date (blank while the
    last session is still open), total = sum of completed sessions."""
    ins = [s.in_ts for s in sessions if s.in_ts]
    first_in = min(ins) if ins else None
    last = max(sessions, key=lambda s: s.in_ts or s.out_ts) if sessions else None
    last_out = last.out_ts if last is not None else None  # None while the last session is open
    return {"first_in": first_in, "last_out": last_out, "seconds": sum(s.seconds for s in sessions),
            "open": any(s.open for s in sessions), "sessions": len(sessions),
            "flags": sorted({f for s in sessions for f in s.flags})}


def fmt_duration(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m"


# ---------- punching ----------
def _lock_employee(db: Session, emp_pk: int) -> Employee:
    return db.execute(select(Employee).where(Employee.id == emp_pk).with_for_update()).scalar_one()


def record_punch(db: Session, settings: Settings, emp: Employee, event_type: str, *, client_ip: str | None,
                 request_key: str | None) -> tuple[AttendanceEvent, AttendanceRemark | None]:
    """Validated QR punch from ANY network (office, home, mobile data). Timestamp is the server clock. The event and
    any required remark request are committed in ONE transaction. Returns (event, remark-or-None)."""
    from app.services import remark_service

    now = now_utc()
    if event_type not in ("IN", "OUT"):
        raise PunchError("bad_type", "Invalid punch type", 422)
    with _sqlite_lock:
        emp = _lock_employee(db, emp.id)
        meta = {"type": event_type, "client_ip": client_ip}
        if emp.account_status != "ACTIVE":
            audit(db, emp.verified_email, "PUNCH_REJECTED", "employee", emp.employee_id, {**meta, "why": "inactive"})
            db.commit()
            raise PunchError("inactive", "Your account is not active. Contact HR.", 403)
        if request_key:
            dup = db.scalar(select(AttendanceEvent).where(AttendanceEvent.employee_id == emp.id,
                                                          AttendanceEvent.request_key == request_key))
            if dup:  # idempotent replay of the same click/request
                rem = db.scalar(select(AttendanceRemark).where(AttendanceRemark.attendance_id == dup.attendance_id))
                return dup, rem
        opens = settings.punch_in_opens_time
        if event_type == "IN" and opens and now.astimezone(settings.tz).time() < opens:
            raise PunchError("too_early", f"Punch In opens at {opens.strftime('%I:%M %p')}.", 403)
        recent = _events(db, emp.id, now - timedelta(days=settings.stale_session_hours // 24 + 3),
                         now + timedelta(minutes=1))
        last = recent[-1] if recent else None
        if last and (now - last.event_timestamp).total_seconds() < settings.min_seconds_between_punches:
            raise PunchError("too_fast", "Please wait a moment before punching again.", 429)

        stale_in = None
        if event_type == "IN":
            if last and last.event_type == "IN":
                age_h = (now - last.event_timestamp).total_seconds() / 3600
                if age_h < settings.stale_session_hours:
                    raise PunchError("already_in", "You are already punched in. Punch out first.")
                stale_in = last  # an old IN never closed: stays open (no OUT is invented), a new IN is allowed
        else:
            if not last or last.event_type != "IN":
                raise PunchError("no_open_in", "No open Punch In found. Punch in first.")

        ev = AttendanceEvent(employee_id=emp.id, event_type=event_type, event_timestamp=now, source="QR",
                             network_verified=False, client_ip=client_ip,
                             recorded_by=emp.verified_email, request_key=request_key)
        db.add(ev)
        db.flush()
        if stale_in is not None:
            audit(db, "system", "STALE_SESSION_LEFT_OPEN", "attendance", stale_in.attendance_id,
                  {"employee": emp.employee_id, "in_time": stale_in.event_timestamp.isoformat()})
        remark = remark_service.evaluate_punch(db, settings, emp, ev)
        audit(db, emp.verified_email, f"PUNCH_{event_type}", "employee", emp.employee_id,
              {**meta, "attendance_id": ev.attendance_id,
               "remark_required": remark.exception_type if remark else None})
        try:
            db.commit()
        except IntegrityError:  # concurrent duplicate key
            db.rollback()
            raise PunchError("duplicate", "Duplicate request ignored.", 409)
        return ev, remark


# ---------- read models ----------
def today_view(db: Session, settings: Settings, emp: Employee) -> dict:
    today = now_utc().astimezone(settings.tz).date()
    now = now_utc()
    ss = sessions_for_range(db, settings, today - timedelta(days=1), today, emp.id).get(emp.id, [])
    # Never-closed punch-ins older than STALE_SESSION_HOURS are history, not "currently punched in": record_punch lets
    # the employee punch in again, so the page must offer it too (and must not show yesterday's time as today's).
    stale_cut = now - timedelta(hours=settings.stale_session_hours)
    ss = [s for s in ss if not (s.open and s.in_ts and s.in_ts < stale_cut)]
    # today's work, plus a live (non-stale) open session carried over from yesterday (e.g. night shift) ...
    current = [s for s in ss if s.work_date == today or s.open]
    # ... and only when there is none of that, an overnight session that ended this morning
    ss = current or [s for s in ss if s.out_ts and local_date(s.out_ts, settings) == today]
    summ = summarise_day(ss)
    secs = summ["seconds"] + sum(int((now - s.in_ts).total_seconds()) for s in ss if s.open and s.in_ts)
    shift, issue = sh.resolve_shift(emp, sh.load_shifts(db))
    return {
        "date": today.isoformat(),
        "punch_in": fmt_time(summ["first_in"], settings) or "Not yet punched",
        "punch_out": fmt_time(summ["last_out"], settings) or "Not yet punched",
        "working_hours": "In progress" if summ["open"] else (fmt_duration(secs) if secs else "—"),
        "elapsed": fmt_duration(secs) if secs else None,
        "is_punched_in": summ["open"],
        "punch_in_opens_at": settings.punch_in_opens_time.strftime("%H:%M") if settings.punch_in_opens_time else None,
        "can_punch_in": not settings.punch_in_opens_time or now.astimezone(settings.tz).time() >= settings.punch_in_opens_time,
        "shift": ({"name": shift.name, "start": sh.fmt_clock(shift.start), "end": sh.fmt_clock(shift.end)}
                  if shift else None),
        "shift_issue": issue,
        "sessions": [{"in": fmt_time(s.in_ts, settings), "out": fmt_time(s.out_ts, settings),
                      "work_date": s.work_date.isoformat(), "flags": s.flags} for s in ss],
    }


def history_view(db: Session, settings: Settings, emp: Employee, days: int = 14) -> list[dict]:
    today = now_utc().astimezone(settings.tz).date()
    ss = sessions_for_range(db, settings, today - timedelta(days=days), today, emp.id).get(emp.id, [])
    by: dict[date, list[Session_]] = {}
    for s in ss:
        by.setdefault(s.work_date, []).append(s)
    rows = []
    for d in sorted(by, reverse=True):
        summ = summarise_day(by[d])
        rows.append({"date": d.isoformat(), "in": fmt_time(summ["first_in"], settings),
                     "out": fmt_time(summ["last_out"], settings),
                     "hours": fmt_duration(summ["seconds"]) if summ["seconds"] else None,
                     "flags": summ["flags"]})
    return rows


def active_employees(db: Session) -> list[Employee]:
    return list(db.scalars(select(Employee).where(Employee.account_status == "ACTIVE").order_by(Employee.employee_name)))


def exception_scan(db: Session, settings: Settings, d1: date, d2: date, emp_pk: int | None = None) -> list[dict]:
    """Late arrivals (first IN of a day) and early departures (every closed session) judged against the
    employee's HR-assigned shift and tolerances. Employees without a valid shift are skipped here and
    reported as configuration issues elsewhere."""
    shifts = sh.load_shifts(db)
    emps = {e.id: e for e in db.scalars(select(Employee))}
    remarks = {(r.attendance_id, r.exception_type): r for r in db.scalars(select(AttendanceRemark))}
    out = []
    for pk, ss in sessions_for_range(db, settings, d1, d2, emp_pk).items():
        e = emps[pk]
        shift, _ = sh.resolve_shift(e, shifts)
        if shift is None:
            continue
        seen_days: set[date] = set()
        for s in sorted(ss, key=lambda x: x.in_ts or x.out_ts):
            base = {"employee_id": e.employee_id, "employee_name": e.employee_name, "department": e.department,
                    "shift": shift.name, "work_date": s.work_date.isoformat()}
            if s.in_ts and s.work_date not in seen_days:
                seen_days.add(s.work_date)
                delay = sh.late_delay_minutes(shift, s.in_ts, s.work_date, settings.tz)
                if delay > shift.late_tol:
                    r = remarks.get((s.in_event.attendance_id, LATE_ARRIVAL))
                    out.append({**base, "kind": LATE_ARRIVAL, "variance_minutes": delay, "tolerance": shift.late_tol,
                                "actual": fmt_time(s.in_ts, settings),
                                "scheduled": sh.fmt_clock(shift.start), "attendance_id": s.in_event.attendance_id,
                                "remark_id": r.remark_id if r else None,
                                "remark_status": r.remark_status if r else None,
                                "reason_category": r.reason_category if r else None})
            if s.out_ts and s.out_event is not None:
                early = sh.early_departure_minutes(shift, s.out_ts, s.work_date, settings.tz)
                if early > shift.early_tol:
                    r = remarks.get((s.out_event.attendance_id, EARLY_DEPARTURE))
                    out.append({**base, "kind": EARLY_DEPARTURE, "variance_minutes": early,
                                "tolerance": shift.early_tol, "actual": fmt_time(s.out_ts, settings),
                                "scheduled": sh.fmt_clock(shift.end), "attendance_id": s.out_event.attendance_id,
                                "remark_id": r.remark_id if r else None,
                                "remark_status": r.remark_status if r else None,
                                "reason_category": r.reason_category if r else None})
    out.sort(key=lambda r: (r["work_date"], r["employee_name"]))
    return out


def shift_config_issues(db: Session) -> list[dict]:
    shifts = sh.load_shifts(db)
    return [{"employee_id": e.employee_id, "employee_name": e.employee_name, "issue": issue}
            for e in active_employees(db) if (issue := sh.resolve_shift(e, shifts)[1])]


def _shift_over(shift: sh.ShiftInfo | None, work_date: date, settings: Settings) -> bool:
    """True when the scheduled end of the shift for work_date has passed (False if no shift)."""
    return bool(shift) and now_utc() > sh.scheduled_end(shift, work_date, settings.tz)


def day_overview(db: Session, settings: Settings, d: date, department: str | None = None,
                 shift_name: str | None = None) -> dict:
    """Deterministic figures for a date (used by the dashboard AND the assistant's tools)."""
    shifts = sh.load_shifts(db)
    emps = active_employees(db)
    resolved = {e.id: sh.resolve_shift(e, shifts) for e in emps}
    if department:
        emps = [e for e in emps if (e.department or "").lower() == department.lower()]
    if shift_name:
        emps = [e for e in emps if (resolved[e.id][0].name.lower() if resolved[e.id][0] else "") == shift_name.lower()]
    ids = {e.id for e in emps}
    sess = sessions_for_range(db, settings, d, d)
    exc = [x for x in exception_scan(db, settings, d, d)]
    late = {x["employee_id"]: x for x in exc if x["kind"] == LATE_ARRIVAL}
    early = {x["employee_id"]: x for x in exc if x["kind"] == EARLY_DEPARTURE}
    today = now_utc().astimezone(settings.tz).date()
    rows, present, no_in, missing_out = [], [], [], []
    for e in emps:
        ss = sess.get(e.id, [])
        summ = summarise_day(ss)
        shift, issue = resolved[e.id]
        rows.append({"employee_id": e.employee_id, "employee_name": e.employee_name, "department": e.department,
                     "shift": shift.name if shift else None, "shift_issue": issue,
                     "in": fmt_time(summ["first_in"], settings), "out": fmt_time(summ["last_out"], settings),
                     "hours": fmt_duration(summ["seconds"]) if summ["seconds"] else None,
                     "late_minutes": late[e.employee_id]["variance_minutes"] if e.employee_id in late else None,
                     "early_minutes": early[e.employee_id]["variance_minutes"] if e.employee_id in early else None,
                     "open": summ["open"], "flags": summ["flags"]})
        if ss:
            present.append(e.employee_id)
        else:
            no_in.append(e.employee_id)
        if summ["open"] and (d < today or _shift_over(shift, d, settings)):
            missing_out.append(e.employee_id)
    mine = lambda x: x["employee_id"] in {e.employee_id for e in emps}  # noqa: E731
    exc = [x for x in exc if mine(x)]
    pend = db.scalars(select(AttendanceRemark).where(AttendanceRemark.remark_status == PENDING_REMARK)).all()
    by_dept: dict[str, dict] = {}
    by_shift: dict[str, dict] = {}
    for r in rows:
        for bucket, key in ((by_dept, r["department"] or "(none)"), (by_shift, r["shift"] or "(no valid shift)")):
            b = bucket.setdefault(key, {"total": 0, "present": 0})
            b["total"] += 1
            b["present"] += r["employee_id"] in present
    return {"date": d.isoformat(), "total_active_employees": len(emps),
            "punched_in": present, "not_punched_in": no_in, "missing_punch_out": missing_out,
            "currently_working": [r["employee_id"] for r in rows if r["open"] and d == today],
            "late_arrivals": sorted(late.keys() & {e.employee_id for e in emps}),
            "early_departures": sorted(early.keys() & {e.employee_id for e in emps}),
            "remarks_pending": sum(1 for x in exc if x["remark_status"] == PENDING_REMARK),
            "remarks_submitted": sum(1 for x in exc if x["remark_status"] == "Remark Submitted"),
            "pending_remarks_total": len([p for p in pend if p.employee_id in ids]),
            "shift_config_issues": [i for i in shift_config_issues(db) if i["employee_id"] in {e.employee_id for e in emps}],
            "by_department": by_dept, "by_shift": by_shift, "rows": rows,
            "note": "Late/early are only evaluated for employees with a valid HR-assigned shift."}


def monthly_figures(db: Session, settings: Settings, month: str) -> dict:
    """Per-employee monthly figures (deterministic). month = 'YYYY-MM'."""
    y, m = (int(x) for x in month.split("-"))
    today = now_utc().astimezone(settings.tz).date()
    first = date(y, m, 1)
    last = min(date(y, m, calendar.monthrange(y, m)[1]), today)
    if last < first:
        return {"month": month, "employees": []}
    sess = sessions_for_range(db, settings, first, last)
    exc = exception_scan(db, settings, first, last)
    pend = {}
    for x in exc:
        if x["remark_status"] == PENDING_REMARK:
            pend[x["employee_id"]] = pend.get(x["employee_id"], 0) + 1
    shifts = sh.load_shifts(db)
    out = []
    for e in db.scalars(select(Employee).order_by(Employee.employee_name)):
        if e.account_status != "ACTIVE" and e.id not in sess:
            continue
        by: dict[date, list[Session_]] = {}
        for s in sess.get(e.id, []):
            by.setdefault(s.work_date, []).append(s)
        c = {"present": 0, "absent": 0,
             "missing_out": 0, "missing_in": 0, "seconds": 0, "working_days": 0}
        missing_dates: list[str] = []
        for i in range((last - first).days + 1):
            d = first + timedelta(days=i)
            workday = d.weekday() in settings.working_day_set
            c["working_days"] += workday
            if d in by:
                summ = summarise_day(by[d])
                c["present"] += 1
                c["seconds"] += summ["seconds"]
                if "MISSING_OUT" in summ["flags"] or (summ["open"] and d < today):
                    c["missing_out"] += 1
                    missing_dates.append(f"OUT missing {d:%d-%b}")
                if "MISSING_IN" in summ["flags"]:
                    c["missing_in"] += 1
                    missing_dates.append(f"IN missing {d:%d-%b}")
            elif workday and d < today:
                c["absent"] += 1
        mine = [x for x in exc if x["employee_id"] == e.employee_id]
        shift = sh.resolve_shift(e, shifts)[0]
        out.append({"employee_id": e.employee_id, "employee_name": e.employee_name, "department": e.department,
                    "shift": shift.name if shift else None, "total_hours": round(c.pop("seconds") / 3600, 2), **c,
                    "late": sum(1 for x in mine if x["kind"] == LATE_ARRIVAL),
                    "early": sum(1 for x in mine if x["kind"] == EARLY_DEPARTURE),
                    "pending_remarks": pend.get(e.employee_id, 0), "missing_indicator": "; ".join(missing_dates),
                    "status": "Review" if (c["missing_out"] or c["missing_in"]) else
                    ("Absences" if c["absent"] else "OK")})
    return {"month": month, "employees": out}
