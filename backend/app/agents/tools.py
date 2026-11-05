"""The 12 controlled tools available to the ONE combined (employee + HR) Domo assistant.

Security model
  * Identity and role come from the authenticated backend session (ToolContext) - never from chat text.
  * EMPLOYEE role: tools that take an employee operate ONLY on ctx.employee; passing employee_id is refused.
    Employees get no org-wide / investigative / mutating tools (they are not even listed in the catalog).
  * Admin roles: employee_id is required; the one mutating tool needs SUPER_ADMIN / HR_ADMIN (AUDITOR is read-only).
  * SYSTEM role: used only by the scheduled monitor; read-only tools.
  * Every call is validated, authorised, executed and audited as "agent:<actor>" in ONE choke point (call_tool).
  * No tool can write attendance_events or change attendance_remarks.remark_status. The only write is workbook
    regeneration (generate_attendance_report).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import (
    AttendanceRemark, Employee, EARLY_DEPARTURE, LATE_ARRIVAL, PENDING_REMARK, REMARK_SUBMITTED,
)
from app.services import attendance_service as att
from app.services import shift_service as sh
from app.services.audit import audit

MAX_RANGE_DAYS = 92
EMP, ADMIN_ROLES = "EMPLOYEE", ("SUPER_ADMIN", "HR_ADMIN", "AUDITOR")


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    db: Session
    settings: Settings
    actor: str                      # e-mail of the authenticated user, or "system"
    role: str                       # EMPLOYEE | SUPER_ADMIN | HR_ADMIN | AUDITOR | SYSTEM
    employee: Employee | None = None  # set only for role == EMPLOYEE

    @property
    def is_employee(self) -> bool:
        return self.role == EMP

    @property
    def agent_actor(self) -> str:
        return f"agent:{self.actor}"


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, dict]
    handler: Callable[[ToolContext, dict], Any]
    scope: str = "admin"            # "self_or_admin" | "admin"
    writers: tuple[str, ...] = ()   # roles allowed to call a MUTATING tool (empty = read-only tool)


# ---------------- validation ----------------
def _validate(spec: Tool, args: Any) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = set(args) - set(spec.params)
    if unknown:
        raise ToolError(f"unknown arguments: {sorted(unknown)}")
    clean: dict = {}
    for k, p in spec.params.items():
        v = args.get(k)
        if v is None or v == "":
            if p.get("required"):
                raise ToolError(f"missing required argument: {k}")
            continue
        t = p["type"]
        if t == "date":
            try:
                clean[k] = date.fromisoformat(str(v))
            except ValueError:
                raise ToolError(f"{k} must be YYYY-MM-DD")
        elif t == "int":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise ToolError(f"{k} must be an integer")
            if not (p.get("min", 1) <= iv <= p.get("max", 1_000_000)):
                raise ToolError(f"{k} out of range")
            clean[k] = iv
        elif t == "employee":
            if not isinstance(v, str) or not 2 <= len(v.strip()) <= 32 or not v.replace("-", "").replace("_", "").isalnum():
                raise ToolError(f"{k} must be a valid employee id")
            clean[k] = v.strip().upper()
        elif t == "enum":
            if v not in p["values"]:
                raise ToolError(f"{k} must be one of {p['values']}")
            clean[k] = v
        else:
            if not isinstance(v, str):
                raise ToolError(f"{k} must be a string")
            clean[k] = v.strip()[: p.get("max", 500)]
    return clean


def _target(ctx: ToolContext, a: dict) -> Employee:
    """Who is the tool about? Employees: always themselves. Admins: the id they asked for."""
    if ctx.is_employee:
        if "employee_id" in a:
            raise ToolError("Employees can only access their own records; do not pass employee_id.")
        if ctx.employee is None:
            raise ToolError("no authenticated employee")
        return ctx.employee
    if "employee_id" not in a:
        raise ToolError("missing required argument: employee_id")
    e = ctx.db.scalar(select(Employee).where(Employee.employee_id == a["employee_id"]))
    if not e:
        raise ToolError(f"employee {a['employee_id']} not found")
    return e


def _range(a: dict, ctx: ToolContext, default_days: int = 7) -> tuple[date, date]:
    today = att.now_utc().astimezone(ctx.settings.tz).date()
    d2 = a.get("date_to", today)
    d1 = a.get("date_from", d2 - timedelta(days=default_days - 1))
    if d1 > d2 or (d2 - d1).days >= MAX_RANGE_DAYS:
        raise ToolError(f"date range invalid or longer than {MAX_RANGE_DAYS} days")
    return d1, d2


def _hhmm(ts, ctx):
    return att.fmt_time(ts, ctx.settings)


# ---------------- tool handlers ----------------
def get_employee_details(ctx, a):
    e = _target(ctx, a)
    return {"employee_id": e.employee_id, "employee_name": e.employee_name, "email": e.verified_email,
            "department": e.department, "designation": e.designation, "location": e.location,
            "assigned_shift": e.assigned_shift, "account_status": e.account_status}


def get_employee_shift(ctx, a):
    e = _target(ctx, a)
    shift, issue = sh.resolve_shift(e, sh.load_shifts(ctx.db))
    if shift is None:
        return {"employee_id": e.employee_id, "assigned_shift": e.assigned_shift, "valid": False, "issue": issue,
                "note": "No valid shift mapping. Do not assume any schedule; this is a configuration issue for HR."}
    return {"employee_id": e.employee_id, "valid": True, "shift_name": shift.name,
            "start": sh.fmt_clock(shift.start), "end": sh.fmt_clock(shift.end), "overnight": shift.overnight,
            "late_tolerance_minutes": shift.late_tol, "early_departure_tolerance_minutes": shift.early_tol}


def get_attendance_records(ctx, a):
    e = _target(ctx, a)
    d1, d2 = _range(a, ctx, 14)
    s = ctx.settings
    sess = att.sessions_for_range(ctx.db, s, d1, d2, e.id).get(e.id, [])
    by: dict[date, list] = {}
    for x in sess:
        by.setdefault(x.work_date, []).append(x)
    days = []
    for d, group in sorted(by.items()):
        summ = att.summarise_day(group)
        days.append({"date": d.isoformat(), "first_in": _hhmm(summ["first_in"], ctx), "last_out": _hhmm(summ["last_out"], ctx),
                     "worked": att.fmt_duration(summ["seconds"]), "open_session": summ["open"], "flags": summ["flags"],
                     "sessions": [{"attendance_id_in": x.in_event.attendance_id if x.in_event else None,
                                   "in": _hhmm(x.in_ts, ctx), "out": _hhmm(x.out_ts, ctx),
                                   "source": x.in_event.source if x.in_event else None} for x in group]})
    return {"employee_id": e.employee_id, "from": d1.isoformat(), "to": d2.isoformat(), "days": days,
            "late_or_early_events": att.exception_scan(ctx.db, s, d1, d2, e.id)}


def _remark_row(r: AttendanceRemark, ctx) -> dict:
    return {"remark_id": r.remark_id, "attendance_id": r.attendance_id, "type": r.exception_type,
            "remark_status": r.remark_status, "shift": r.shift_name, "variance_minutes": r.variance_minutes,
            "scheduled": _hhmm(r.scheduled_time, ctx), "actual": _hhmm(r.event.event_timestamp, ctx),
            "date": r.scheduled_time.astimezone(ctx.settings.tz).date().isoformat(),
            "reason_category": r.reason_category,
            "employee_provided_text": r.employee_remark,  # UNVERIFIED claim by the employee
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None}


def get_employee_remarks(ctx, a):
    e = _target(ctx, a)
    rows = ctx.db.scalars(select(AttendanceRemark).where(AttendanceRemark.employee_id == e.id)
                          .order_by(AttendanceRemark.remark_id.desc()).limit(100)).all()
    if a.get("status"):
        rows = [r for r in rows if r.remark_status == a["status"]]
    return {"employee_id": e.employee_id, "remarks": [_remark_row(r, ctx) for r in rows],
            "note": "employee_provided_text is the employee's own unverified statement."}


def get_attendance_policy(ctx, a):
    s = ctx.settings
    rules = [
        "Employees can punch from any network (office, home or mobile data). Punching is one tap: Punch In to start work, "
        "Punch Out to end it. Work location is not recorded.",
        "Official punch times are the server's clock; they cannot be edited by employees or by the assistant.",
        "Late arrival / early departure is judged against the employee's HR-assigned shift and that shift's tolerance in minutes; "
        "outside tolerance the employee is asked for a remark. A remark request is not a finding of fault.",
        "Remark status is either 'Pending Employee Remark' or 'Remark Submitted'. HR/AI review outcomes are tracked separately.",
        "Missing punch: tell HR. A missing Punch Out is never filled in automatically and the assistant cannot change punch records.",
        f"Session policy: an open session older than {s.stale_session_hours} hours is reported as a missing Punch Out.",
    ]
    text = None
    if s.attendance_policy_file:
        try:
            text = Path(s.attendance_policy_file).read_text(encoding="utf-8")[:6000]
        except OSError:
            text = None
    out = {"system_rules": rules, "hr_policy_text": text,
           "note": None if text else "HR has not published additional policy text; only the system rules above are known."}
    if ctx.is_employee and ctx.employee:
        out["your_shift"] = get_employee_shift(ctx, {})
    elif not ctx.is_employee:
        out["shifts"] = [{"name": x.name, "start": sh.fmt_clock(x.start), "end": sh.fmt_clock(x.end),
                          "late_tolerance_minutes": x.late_tol, "early_tolerance_minutes": x.early_tol}
                         for x in sh.load_shifts(ctx.db).values()]
    return out


def detect_missing_punches(ctx, a):
    d1, d2 = _range(a, ctx)
    s = ctx.settings
    shifts = sh.load_shifts(ctx.db)
    emps = {e.id: e for e in att.active_employees(ctx.db)}
    if a.get("employee_id"):
        emps = {e.id: e for e in [_target(ctx, a)]}
    found = []
    for pk, ss in att.sessions_for_range(ctx.db, s, d1, d2).items():
        e = emps.get(pk)
        if not e:
            continue
        shift = sh.resolve_shift(e, shifts)[0]
        for x in ss:
            base = {"employee_id": e.employee_id, "employee_name": e.employee_name, "work_date": x.work_date.isoformat()}
            if "MISSING_OUT" in x.flags or (x.open and (x.work_date < att.now_utc().astimezone(s.tz).date()
                                                          or att._shift_over(shift, x.work_date, s))):
                found.append({**base, "type": "MISSING_OUT", "in_time": _hhmm(x.in_ts, ctx),
                              "shift": shift.name if shift else None, "shift_end_passed": True if shift else None})
            elif x.open:
                found.append({**base, "type": "OPEN_SESSION", "in_time": _hhmm(x.in_ts, ctx),
                              "shift": shift.name if shift else None, "shift_end_passed": False if shift else None,
                              "note": None if shift else "no valid shift: cannot tell whether the shift is over"})
            if "MISSING_IN" in x.flags:
                found.append({**base, "type": "MISSING_IN", "out_time": _hhmm(x.out_ts, ctx)})
    return {"from": d1.isoformat(), "to": d2.isoformat(), "count": len(found), "items": found}


def _scan(ctx, a, kind):
    d1, d2 = _range(a, ctx)
    pk = _target(ctx, a).id if a.get("employee_id") else None
    rows = [r for r in att.exception_scan(ctx.db, ctx.settings, d1, d2, pk) if r["kind"] == kind]
    if a.get("department"):
        rows = [r for r in rows if (r["department"] or "").lower() == a["department"].lower()]
    return {"from": d1.isoformat(), "to": d2.isoformat(), "count": len(rows), "items": rows[:200],
            "note": "Only employees with a valid HR-assigned shift are evaluated."}


def detect_late_arrivals(ctx, a):
    return _scan(ctx, a, LATE_ARRIVAL)


def detect_early_departures(ctx, a):
    return _scan(ctx, a, EARLY_DEPARTURE)


def investigate_attendance_exception(ctx, a):
    db = ctx.db
    if not a.get("remark_id") and not a.get("attendance_id"):
        raise ToolError("provide remark_id or attendance_id")
    q = select(AttendanceRemark)
    q = q.where(AttendanceRemark.remark_id == a["remark_id"]) if a.get("remark_id") else \
        q.where(AttendanceRemark.attendance_id == a["attendance_id"])
    remarks = db.scalars(q).all()
    if not remarks:
        raise ToolError("no remark/exception found for that id")
    out = []
    for r in remarks:
        e, ev, s = r.employee, r.event, ctx.settings
        wd = r.scheduled_time.astimezone(s.tz).date()
        since = att.now_utc() - timedelta(days=30)
        hist = db.scalars(select(AttendanceRemark).where(AttendanceRemark.employee_id == e.id,
                                                         AttendanceRemark.created_at >= since)).all()
        out.append({
            "verified_evidence": {
                "employee": {"employee_id": e.employee_id, "department": e.department},
                "attendance_event": {"attendance_id": ev.attendance_id, "type": ev.event_type,
                                     "time": _hhmm(ev.event_timestamp, ctx), "source": ev.source,
                                     "recorded_by": ev.recorded_by},
                "shift": {"name": r.shift_name, "scheduled": _hhmm(r.scheduled_time, ctx),
                          "variance_minutes": r.variance_minutes, "exception_type": r.exception_type},
                "history_30d": {"late_requests": sum(1 for h in hist if h.exception_type == LATE_ARRIVAL),
                                "early_requests": sum(1 for h in hist if h.exception_type == EARLY_DEPARTURE),
                                "remarks_submitted": sum(1 for h in hist if h.remark_status == REMARK_SUBMITTED),
                                "remarks_pending": sum(1 for h in hist if h.remark_status == PENDING_REMARK)},
                "note": "This system holds no schedule-adjustment, leave or approval records, so none can be confirmed."},
            "employee_provided_unverified": {"remark_status": r.remark_status, "reason_category": r.reason_category,
                                             "statement": r.employee_remark,
                                             "warning": "This is the employee's own claim. Do not treat it as verified, "
                                                        "and do not infer dishonesty from the absence of corroboration."},
            "cannot_be_verified_from_system_data": ["traffic, health or personal circumstances"]})
    return {"remark_id": remarks[0].remark_id, "investigations": out}


def analyse_attendance_pattern(ctx, a):
    e = _target(ctx, a)
    days = a.get("days", 30)
    s = ctx.settings
    today = att.now_utc().astimezone(s.tz).date()
    d1 = today - timedelta(days=days - 1)
    rows = att.exception_scan(ctx.db, s, d1, today, e.id)
    late = [r for r in rows if r["kind"] == LATE_ARRIVAL]
    early = [r for r in rows if r["kind"] == EARLY_DEPARTURE]
    wk: dict[str, int] = {}
    for r in late:
        wk[date.fromisoformat(r["work_date"]).strftime("%A")] = wk.get(date.fromisoformat(r["work_date"]).strftime("%A"), 0) + 1
    sess = att.sessions_for_range(ctx.db, s, d1, today, e.id).get(e.id, [])
    shift, issue = sh.resolve_shift(e, sh.load_shifts(ctx.db))
    cats: dict[str, int] = {}
    for r in rows:
        if r["reason_category"]:
            cats[r["reason_category"]] = cats.get(r["reason_category"], 0) + 1
    return {"employee_id": e.employee_id, "window_days": days, "shift": shift.name if shift else None,
            "shift_issue": issue, "days_present": len({x.work_date for x in sess}),
            "late_count": len(late), "late_dates": [r["work_date"] for r in late],
            "avg_minutes_late": round(sum(r["variance_minutes"] for r in late) / len(late), 1) if late else None,
            "late_by_weekday": wk, "early_count": len(early), "early_dates": [r["work_date"] for r in early],
            "avg_minutes_early": round(sum(r["variance_minutes"] for r in early) / len(early), 1) if early else None,
            "missing_punch_out_dates": [x.work_date.isoformat() for x in sess if "MISSING_OUT" in x.flags],
            "remark_reason_categories": cats,
            "pending_remarks": sum(1 for r in rows if r["remark_status"] == PENDING_REMARK),
            "note": None if shift else "No valid shift: lateness/early departure cannot be assessed."}


def generate_daily_attendance_summary(ctx, a):
    d = a.get("date") or att.now_utc().astimezone(ctx.settings.tz).date()
    o = att.day_overview(ctx.db, ctx.settings, d, a.get("department"), a.get("shift"))
    names = {r["employee_id"]: r["employee_name"] for r in o["rows"]}
    nm = lambda ids: [{"employee_id": i, "employee_name": names.get(i)} for i in ids]  # noqa: E731
    return {"date": o["date"], "total_active_employees": o["total_active_employees"],
            "punched_in_count": len(o["punched_in"]), "not_punched_in": nm(o["not_punched_in"]),
            "missing_punch_out": nm(o["missing_punch_out"]), "currently_working_count": len(o["currently_working"]),
            "late_arrivals": nm(o["late_arrivals"]), "early_departures": nm(o["early_departures"]),
            "remarks_pending": o["remarks_pending"], "remarks_submitted": o["remarks_submitted"],
            "by_department": o["by_department"], "by_shift": o["by_shift"],
            "shift_config_issues": o["shift_config_issues"], "note": o["note"]}


def generate_attendance_report(ctx, a):
    from app.services.excel_generator import generate_report
    path = generate_report(ctx.db, ctx.settings)
    return {"generated": True, "file": path.name, "note": "Available to admins via the Admin Portal download only."}


E_OPT = {"type": "employee"}
D = {"type": "date"}
DEPT = {"type": "str", "max": 100}
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_employee_details", "Get profile details of one employee.", {"employee_id": E_OPT},
         get_employee_details, "self_or_admin"),
    Tool("get_employee_shift", "Get the HR-assigned shift (start, end, tolerances, overnight) or the configuration issue.",
         {"employee_id": E_OPT}, get_employee_shift, "self_or_admin"),
    Tool("get_attendance_records", "Get punch sessions per day plus late/early events for a date range (default 14 days).",
         {"employee_id": E_OPT, "date_from": D, "date_to": D}, get_attendance_records, "self_or_admin"),
    Tool("get_employee_remarks", "Get late/early remark requests, their two-value status and the employee's submitted text.",
         {"employee_id": E_OPT, "status": {"type": "enum", "values": [PENDING_REMARK, REMARK_SUBMITTED]}},
         get_employee_remarks, "self_or_admin"),
    Tool("get_attendance_policy", "Get the attendance policy: system rules, optional HR policy text, shifts.", {},
         get_attendance_policy, "self_or_admin"),
    Tool("detect_missing_punches", "Find sessions with IN but no OUT (or OUT without IN) in a range, optionally for one employee.",
         {"date_from": D, "date_to": D, "employee_id": E_OPT}, detect_missing_punches),
    Tool("detect_late_arrivals", "List late arrivals (beyond shift tolerance) with remark status, for a range/employee/department.",
         {"date_from": D, "date_to": D, "employee_id": E_OPT, "department": DEPT}, detect_late_arrivals),
    Tool("detect_early_departures", "List early punch-outs (beyond shift tolerance) with remark status, for a range/employee/department.",
         {"date_from": D, "date_to": D, "employee_id": E_OPT, "department": DEPT}, detect_early_departures),
    Tool("investigate_attendance_exception",
         "Collect verified evidence vs employee-provided statement for a late/early remark (by remark_id or attendance_id).",
         {"remark_id": {"type": "int"}, "attendance_id": {"type": "int"}}, investigate_attendance_exception),
    Tool("analyse_attendance_pattern", "Late / early / missing-punch pattern for one employee over N days (7-90).",
         {"employee_id": {"type": "employee", "required": True}, "days": {"type": "int", "min": 7, "max": 90}},
         analyse_attendance_pattern),
    Tool("generate_daily_attendance_summary", "Official figures for a date (optionally by department / shift name).",
         {"date": D, "department": DEPT, "shift": {"type": "str", "max": 100}}, generate_daily_attendance_summary),
    Tool("generate_attendance_report", "Regenerate the Excel workbook (HR admins only).", {},
         generate_attendance_report, "admin", ("SUPER_ADMIN", "HR_ADMIN")),
]}


def _allowed(spec: Tool, role: str) -> bool:
    if role == EMP:
        return spec.scope == "self_or_admin"
    if spec.writers and role not in spec.writers:
        return False
    return True


def tool_catalog(role: str) -> list[dict]:
    """Only the tools this role may call are described to the model."""
    cat = []
    for t in TOOLS.values():
        if not _allowed(t, role):
            continue
        params = {k: v for k, v in t.params.items() if not (role == EMP and k == "employee_id")}
        cat.append({"name": t.name, "description": t.description, "arguments": params})
    return cat


def call_tool(ctx: ToolContext, name: str, args: Any) -> dict:
    """Single choke point for agent tool use: authorise, validate, execute, audit."""
    spec = TOOLS.get(name)
    meta = {"tool": name, "role": ctx.role, "args": str(args)[:500]}
    try:
        if spec is None:
            raise ToolError(f"unknown tool: {name}")
        if ctx.role != EMP and ctx.role not in ADMIN_ROLES and ctx.role != "SYSTEM":
            raise ToolError("not authorised")
        if not _allowed(spec, ctx.role):
            raise ToolError(f"your role ({ctx.role}) may not use {name}")
        clean = _validate(spec, args)
        result = spec.handler(ctx, clean)
        audit(ctx.db, ctx.agent_actor, "AGENT_TOOL_CALL", "tool", name, {**meta, "ok": True})
        ctx.db.commit()
        return {"ok": True, "result": result}
    except ToolError as exc:
        ctx.db.rollback()
        audit(ctx.db, ctx.agent_actor, "AGENT_TOOL_CALL", "tool", name, {**meta, "ok": False, "error": str(exc)})
        ctx.db.commit()
        return {"ok": False, "error": str(exc)}

