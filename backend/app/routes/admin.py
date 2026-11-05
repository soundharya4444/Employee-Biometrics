"""Admin API - exactly what the six admin pages use: Dashboard (+Excel download), Attendance, Monthly, Remarks,
Employees, Assistant. Every endpoint enforces approved-admin authorisation server-side."""
from __future__ import annotations

import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agents import tools as agent_tools
from app.agents.domo_agent import AssistantAgent
from app.agents.domo_client import DomoClient
from app.auth.deps import csrf_protect, rate_limit, require_admin, require_identity, settings_dep
from app.config import Settings
from app.database.connection import get_db
from app.database.models import AdminUser, AttendanceRemark, AuditLog, Employee, REMARK_STATUSES
from app.services import attendance_service as att
from app.services import excel_generator, monitor
from app.services import shift_service as sh
from app.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])
READ = Depends(require_admin(write=False))
WRITE = Depends(require_admin(write=True))
CSRF = Depends(csrf_protect)


def _d(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "Dates must be YYYY-MM-DD")


def _today(settings: Settings) -> date:
    return att.now_utc().astimezone(settings.tz).date()


@router.get("/api/me")
def whoami(admin: AdminUser = READ, ident: dict = Depends(require_identity)):
    return {"email": admin.email, "role": admin.role, "csrf": ident["csrf"]}


# ---- Dashboard ----
@router.get("/api/dashboard")
def dashboard(date_: str | None = Query(None, alias="date"), department: str | None = None, shift: str | None = None,
              _a: AdminUser = READ, db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    return att.day_overview(db, settings, _d(date_, _today(settings)), department, shift)


@router.get("/api/report/download")
def download_report(scope: str = Query("full", pattern="^(full|attendance)$"), date_from: str | None = None,
                    date_to: str | None = None, admin: AdminUser = READ, db: Session = Depends(get_db),
                    settings: Settings = Depends(settings_dep)):
    """scope=full: the whole workbook (all sheets). scope=attendance: ONLY the Daily Attendance sheet,
    optionally limited to date_from..date_to (YYYY-MM-DD)."""
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if scope == "attendance":
        d1 = _d(date_from, date.min) if date_from else None
        d2 = _d(date_to, date.max) if date_to else None
        if d1 and d2 and (d2 < d1 or (d2 - d1).days > 366):
            raise HTTPException(422, "Invalid date range (max 366 days)")
        data = excel_generator.attendance_only_bytes(db, settings, d1, d2)
        audit(db, admin.email, "REPORT_DOWNLOAD", "report", "attendance-only",
              {"scope": "attendance", "from": date_from, "to": date_to})
        db.commit()
        name = f"attendance_only_{_today(settings):%Y%m%d}.xlsx"
        return Response(data, media_type=xlsx, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                        "Cache-Control": "no-store"})
    path = excel_generator.generate_report(db, settings)  # always fresh at download time
    audit(db, admin.email, "REPORT_DOWNLOAD", "report", path.name, {"scope": "full"})
    db.commit()
    return FileResponse(path, filename=f"attendance_full_{_today(settings):%Y%m%d}.xlsx", media_type=xlsx,
                        headers={"Cache-Control": "no-store"})


# ---- Attendance ----
@router.get("/api/attendance")
def attendance_search(date_from: str | None = None, date_to: str | None = None, employee: str | None = None,
                      department: str | None = None, shift: str | None = None,
                      _a: AdminUser = READ, db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    t = _today(settings)
    d1, d2 = _d(date_from, t - timedelta(days=6)), _d(date_to, t)
    if d2 < d1 or (d2 - d1).days > 366:
        raise HTTPException(422, "Invalid date range (max 366 days)")
    q = select(Employee).order_by(Employee.employee_name)
    if employee:
        like = f"%{employee.strip().lower()}%"
        q = q.where(or_(func.lower(Employee.employee_name).like(like), func.lower(Employee.employee_id).like(like),
                        func.lower(Employee.verified_email).like(like)))
    if department:
        q = q.where(func.lower(Employee.department) == department.lower())
    if shift:
        q = q.where(func.lower(Employee.assigned_shift) == shift.lower())
    emps = {e.id: e for e in db.scalars(q)}
    sess = att.sessions_for_range(db, settings, d1, d2)
    rows = []
    for pk, ss in sess.items():
        e = emps.get(pk)
        if not e:
            continue
        by: dict[date, list] = {}
        for s in ss:
            by.setdefault(s.work_date, []).append(s)
        for d, group in by.items():
            summ = att.summarise_day(group)
            rows.append({"date": d.isoformat(), "employee_id": e.employee_id, "employee_name": e.employee_name,
                         "department": e.department, "shift": e.assigned_shift,
                         "in": att.fmt_time(summ["first_in"], settings), "out": att.fmt_time(summ["last_out"], settings),
                         "hours": att.fmt_duration(summ["seconds"]) if summ["seconds"] else None,
                         "sessions": summ["sessions"], "flags": summ["flags"], "open": summ["open"]})
    rows.sort(key=lambda r: (r["date"], r["employee_name"]), reverse=True)
    return {"rows": rows}


# ---- Monthly ----
@router.get("/api/monthly")
def monthly(month: str | None = None, _a: AdminUser = READ, db: Session = Depends(get_db),
            settings: Settings = Depends(settings_dep)):
    if month and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(422, "month must be YYYY-MM")
    return att.monthly_figures(db, settings, month or _today(settings).strftime("%Y-%m"))


# ---- Remarks (read-only for HR; employees submit their own) ----
@router.get("/api/remarks")
def remarks(status: str | None = None, kind: str | None = None, employee: str | None = None,
            date_from: str | None = None, date_to: str | None = None, _a: AdminUser = READ,
            db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    if status and status not in REMARK_STATUSES:
        raise HTTPException(422, f"status must be one of {list(REMARK_STATUSES)}")
    q = select(AttendanceRemark).join(Employee, Employee.id == AttendanceRemark.employee_id)
    if status:
        q = q.where(AttendanceRemark.remark_status == status)
    if kind:
        q = q.where(AttendanceRemark.exception_type == kind.upper())
    if employee:
        like = f"%{employee.strip().lower()}%"
        q = q.where(or_(func.lower(Employee.employee_name).like(like), func.lower(Employee.employee_id).like(like)))
    if date_from:
        q = q.where(AttendanceRemark.scheduled_time >= att.day_bounds(_d(date_from, date.min), settings)[0])
    if date_to:
        q = q.where(AttendanceRemark.scheduled_time < att.day_bounds(_d(date_to, date.max), settings)[1])
    out = []
    for r in db.scalars(q.order_by(AttendanceRemark.remark_id.desc()).limit(500)):
        out.append({"remark_id": r.remark_id, "employee_id": r.employee.employee_id,
                    "employee_name": r.employee.employee_name, "department": r.employee.department,
                    "type": r.exception_type, "shift": r.shift_name, "variance_minutes": r.variance_minutes,
                    "scheduled": att.fmt_time(r.scheduled_time, settings),
                    "actual": att.fmt_time(r.event.event_timestamp, settings),
                    "date": r.scheduled_time.astimezone(settings.tz).date().isoformat(),
                    "remark_status": r.remark_status, "reason_category": r.reason_category,
                    "employee_remark": r.employee_remark,
                    "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None})
    return {"remarks": out}


# ---- Employees ----
@router.get("/api/employees")
def employees(_a: AdminUser = READ, db: Session = Depends(get_db)):
    shifts = sh.load_shifts(db)
    return {"employees": [{"employee_id": e.employee_id, "employee_name": e.employee_name, "email": e.verified_email,
                           "department": e.department, "status": e.account_status, "registered": e.registered_at is not None,
                           "shift": e.assigned_shift, "shift_issue": sh.resolve_shift(e, shifts)[1]}
                          for e in db.scalars(select(Employee).order_by(Employee.employee_name))]}


class EmpStatusIn(BaseModel):
    status: str = Field(pattern="^(ACTIVE|INACTIVE)$")


@router.post("/api/employees/{employee_id}/status", dependencies=[CSRF])
def employee_status(employee_id: str, body: EmpStatusIn, admin: AdminUser = WRITE, db: Session = Depends(get_db)):
    emp = db.scalar(select(Employee).where(Employee.employee_id == employee_id.upper()))
    if not emp:
        raise HTTPException(404, "Employee not found")
    before = emp.account_status
    emp.account_status = body.status
    audit(db, admin.email, "EMPLOYEE_STATUS", "employee", emp.employee_id, {"from": before, "to": body.status})
    db.commit()
    return {"ok": True}


# ---- Assistant (chat itself is /api/assistant/chat, shared with employees) ----
def _agent(db: Session, settings: Settings, admin: AdminUser) -> AssistantAgent:
    return AssistantAgent(settings, DomoClient(settings), agent_tools.ToolContext(db, settings, admin.email, admin.role))


@router.get("/api/agent/status")
def agent_status(_a: AdminUser = READ, settings: Settings = Depends(settings_dep)):
    c = DomoClient(settings)
    return {"configured": c.is_configured, "missing": c.missing_config(),
            "monitor_enabled": settings.monitor_enabled, "monitor_interval_minutes": settings.monitor_interval_minutes}


@router.post("/api/agent/daily-summary", dependencies=[CSRF, Depends(rate_limit("agent", "punch_rate_limit"))])
def agent_daily(date_: str | None = Query(None, alias="date"), admin: AdminUser = WRITE,
                db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    return _agent(db, settings, admin).daily_summary(_d(date_, _today(settings)))


@router.post("/api/agent/monitor/run", dependencies=[CSRF, Depends(rate_limit("agent", "punch_rate_limit"))])
def agent_monitor_run(admin: AdminUser = WRITE, db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    """Run one monitoring cycle now (the scheduler runs the same code). Forces re-analysis."""
    res = monitor.run_cycle(db, settings, DomoClient(settings), force=True, actor=admin.email)
    return JSONResponse(res, status_code=503) if res.get("status") == "llm_unavailable" else res


@router.get("/api/agent/monitor/latest")
def agent_monitor_latest(_a: AdminUser = READ, db: Session = Depends(get_db)):
    """The most recent scheduled/manual monitoring result (so the Assistant page can show it)."""
    row = db.scalar(select(AuditLog).where(AuditLog.action == monitor.KIND).order_by(AuditLog.audit_id.desc()))
    if not row:
        return {"latest": None}
    m = row.meta or {}
    return {"latest": {"when": row.timestamp.isoformat(), "status": m.get("status"), "answer": m.get("answer"),
                       "counts": m.get("counts"), "tools": m.get("tools"),
                       "human_review_required": m.get("human_review_required"), "error": m.get("error")}}
