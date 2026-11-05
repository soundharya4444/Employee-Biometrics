"""Generates the attendance workbook from the database (the database is the source of truth).

Sheets
  Daily Attendance  S.No | Employee Name | Shift | <date: In Time | Out Time> ...
  Raw Punch Logs    every original event (append-only history)
  Monthly Summary   per employee per month
  Shift Master      HR-maintained shift definitions        (round-trips through hr_import)
  Employee Master   approved employees + assigned shift     (round-trips through hr_import)

Summary policy for a day with several sessions: In Time = first IN of the work date, Out Time = last OUT
of the work date (blank while the last session is open). A valid punch is never replaced by a blank.
Overnight shifts: the OUT is shown under the IN's date (cell shaded yellow). 
HR edits (Shift Master / Employee Master) are validated and applied to the database BEFORE every rebuild,
so regeneration never overwrites HR-maintained values. See hr_import.py.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time as _clock  # (the name `time` below is datetime.time)
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from filelock import FileLock
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.models import AttendanceEvent, Employee, ShiftMaster
from app.services import attendance_service as att
from app.services import hr_import
from app.services import shift_service as sh
from app.services.audit import audit

log = logging.getLogger("attendance.excel")
WORKBOOK_NAME = "attendance.xlsx"
RETRY_SECONDS = 45  # after a failed regeneration (e.g. file open in Excel) try again automatically
TIME_FMT = "hh:mm AM/PM"
HDR_FILL = PatternFill("solid", fgColor="1F3A5F")
SUB_FILL = PatternFill("solid", fgColor="DCE6F1")
NEXT_DAY_FILL = PatternFill("solid", fgColor="FFF2CC")
BAD_FILL = PatternFill("solid", fgColor="F8CBAD")
HR_FILL = PatternFill("solid", fgColor="FFFFE0")
THIN = Side(style="thin", color="BBBBBB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")

status: dict = {"generated_at": None, "error": None, "hr_sync": None}  # surfaced in the admin portal


def report_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    return s.reports_dir / WORKBOOK_NAME


def _t(ts: datetime | None, settings: Settings) -> time | None:
    return ts.astimezone(settings.tz).time().replace(tzinfo=None, microsecond=0) if ts else None


def attendance_only_bytes(db: Session, settings: Settings, d1: date | None = None, d2: date | None = None) -> bytes:
    """In-memory workbook containing ONLY the Daily Attendance sheet (optionally for d1..d2). Nothing is written to
    disk, so it can never clash with the live workbook file."""
    import io
    wb = build_workbook(db, settings, d1, d2, attendance_only=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_workbook(db: Session, settings: Settings, d1: date | None = None, d2: date | None = None,
                   attendance_only: bool = False) -> Workbook:
    today = att.now_utc().astimezone(settings.tz).date()
    emps = list(db.scalars(select(Employee).order_by(Employee.employee_name, Employee.employee_id)))
    emp_by_pk = {e.id: e for e in emps}
    all_events = list(db.scalars(select(AttendanceEvent).order_by(AttendanceEvent.event_timestamp,
                                                                  AttendanceEvent.attendance_id)))
    shifts_active = sh.load_shifts(db)

    events_by_emp: dict[int, list[AttendanceEvent]] = defaultdict(list)
    for ev in all_events:
        events_by_emp[ev.employee_id].append(ev)
    sessions: dict[int, dict[date, list[att.Session_]]] = {}
    work_date_of: dict[int, date] = {}
    for pk, evs in events_by_emp.items():
        d: dict[date, list[att.Session_]] = defaultdict(list)
        for s in att.build_sessions(evs, settings):
            d[s.work_date].append(s)
            for e in (s.in_event, s.out_event):
                if e is not None:
                    work_date_of[e.attendance_id] = s.work_date
        sessions[pk] = d

    dates = sorted({d for m in sessions.values() for d in m} | {today})
    if d1 or d2:  # optional date window (attendance-only download)
        windowed = [d for d in dates if (not d1 or d >= d1) and (not d2 or d <= d2)]
        if not windowed:  # nothing happened in the window: show the requested (blank) calendar days
            lo, hi = (d1 or d2), (d2 or d1)
            windowed = [lo + timedelta(days=i) for i in range(min((hi - lo).days, 366) + 1)]
        dates = windowed
    shown = [e for e in emps if e.account_status == "ACTIVE" or e.id in sessions]

    wb = Workbook()
    _sheet_wide(wb.active, shown, dates, sessions, shifts_active, settings)
    if attendance_only:
        return wb
    _sheet_raw(wb.create_sheet("Raw Punch Logs"), all_events, emp_by_pk, work_date_of, settings)
    _sheet_monthly(wb.create_sheet("Monthly Summary"), db, dates, settings)
    _sheet_shift_master(wb.create_sheet(hr_import.SHIFT_SHEET), db)
    _sheet_employee_master(wb.create_sheet(hr_import.EMP_SHEET), emps, shifts_active)
    return wb


def _sheet_wide(ws, shown, dates, sessions, shifts, settings):
    ws.title = "Daily Attendance"
    for col, label in ((1, "S.No"), (2, "Employee Name"), (3, "Shift")):
        ws.cell(1, col, label)
        ws.merge_cells(start_row=1, start_column=col, end_row=2, end_column=col)
    first = 4
    for i, d in enumerate(dates):
        c = first + i * 2
        cell = ws.cell(1, c, d)
        cell.number_format = "dd-mmm-yyyy"
        ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + 1)
        ws.cell(2, c, "In Time")
        ws.cell(2, c + 1, "Out Time")
        ws.column_dimensions[get_column_letter(c)].width = 12
        ws.column_dimensions[get_column_letter(c + 1)].width = 12
    last_col = first - 1 + len(dates) * 2
    for r in (1, 2):
        for c in range(1, last_col + 1):
            cell = ws.cell(r, c)
            cell.font = Font(bold=True, color="FFFFFF" if r == 1 else "000000")
            cell.fill = HDR_FILL if r == 1 else SUB_FILL
            cell.alignment = CENTER
            cell.border = BORDER
    for n, e in enumerate(shown, start=1):
        r = 2 + n
        ws.cell(r, 1, n).alignment = CENTER
        ws.cell(r, 2, e.employee_name)
        shift, issue = sh.resolve_shift(e, shifts)
        sc = ws.cell(r, 3, shift.name if shift else (f"{e.assigned_shift} (INVALID)" if e.assigned_shift else "NOT ASSIGNED"))
        if not shift:
            sc.fill = BAD_FILL
        for i, d in enumerate(dates):
            c = first + i * 2
            ss = sessions.get(e.id, {}).get(d, [])
            if ss:
                summ = att.summarise_day(ss)
                ti, to = _t(summ["first_in"], settings), _t(summ["last_out"], settings)
                if ti:
                    ic = ws.cell(r, c, ti)
                    ic.number_format = TIME_FMT
                if to:
                    oc = ws.cell(r, c + 1, to)
                    oc.number_format = TIME_FMT
                    if summ["last_out"].astimezone(settings.tz).date() != d:
                        oc.fill = NEXT_DAY_FILL  # overnight: OUT is on the following calendar day
            for cc in (c, c + 1):
                ws.cell(r, cc).alignment = CENTER
                ws.cell(r, cc).border = BORDER
        for cc in (1, 2, 3):
            ws.cell(r, cc).border = BORDER
    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 18
    ws.freeze_panes = "D3"
    ws.cell(4 + len(shown), 2, "Legend: yellow Out Time = punched out on the next calendar day (overnight). "
                               "Several sessions: first IN, last OUT "
                               "(see Raw Punch Logs). Red Shift = not assigned / not in Shift Master. "
                               "Generated from the database - edit only Shift Master / Employee Master."
            ).font = Font(italic=True, size=9)


def _sheet_raw(ws, events, emp_by_pk, work_date_of, settings):
    head = ["Attendance ID", "Employee ID", "Employee Name", "Event", "Local Date", "Local Time",
            "Timestamp (UTC)", "Work Date", "Source", "Network Verified", "Client IP", "Recorded By", "Reason"]
    ws.append(head)
    for ev in events:
        e = emp_by_pk.get(ev.employee_id)
        local = ev.event_timestamp.astimezone(settings.tz)
        ws.append([ev.attendance_id, e.employee_id if e else "", e.employee_name if e else "", ev.event_type,
                   local.date(), local.time().replace(microsecond=0, tzinfo=None),
                   ev.event_timestamp.replace(tzinfo=None), work_date_of.get(ev.attendance_id),
                   ev.source, "Yes" if ev.network_verified else "No", ev.client_ip or "", ev.recorded_by,
                   ev.reason or ""])
    for row in ws.iter_rows(min_row=2):
        row[4].number_format = "dd-mmm-yyyy"
        row[5].number_format = TIME_FMT
        row[6].number_format = "yyyy-mm-dd hh:mm:ss"
        row[7].number_format = "dd-mmm-yyyy"
    _style_table(ws, len(head), [14, 12, 26, 8, 13, 12, 20, 13, 16, 10, 16, 30, 40])


def _sheet_monthly(ws, db, dates, settings):
    head = ["Month", "Employee ID", "Employee Name", "Department", "Shift", "Working Days (to date)",
            "Days Present", "Absent", "Total Hours", "Late Arrivals",
            "Early Departures", "Pending Remarks", "Missing Punch Out", "Missing Punch In", "Attendance Status",
            "Missing-Punch Indicator"]
    ws.append(head)
    for (y, m) in sorted({(d.year, d.month) for d in dates}):
        for r in att.monthly_figures(db, settings, f"{y}-{m:02d}")["employees"]:
            if not (r["present"] or r["absent"]):
                continue
            ws.append([date(y, m, 1), r["employee_id"], r["employee_name"], r["department"] or "", r["shift"] or "",
                       r["working_days"], r["present"],
                       r["absent"], r["total_hours"], r["late"], r["early"], r["pending_remarks"],
                       r["missing_out"], r["missing_in"], r["status"], r["missing_indicator"]])
    for row in ws.iter_rows(min_row=2):
        row[0].number_format = "mmm-yyyy"
    _style_table(ws, len(head), [10, 12, 26, 16, 14, 12, 10, 8, 11, 10, 10, 10, 12, 12, 14, 40])


def _sheet_shift_master(ws, db):
    ws.append(hr_import.SHIFT_HEADERS)
    for s in db.scalars(select(ShiftMaster).order_by(ShiftMaster.shift_name)):
        ws.append([s.shift_name, s.start_time, s.end_time, s.late_tolerance_minutes, s.early_tolerance_minutes,
                   "Yes" if s.active else "No"])
    for row in ws.iter_rows(min_row=2):
        row[1].number_format = TIME_FMT
        row[2].number_format = TIME_FMT
        for c in row:
            c.fill = HR_FILL
    _style_table(ws, len(hr_import.SHIFT_HEADERS), [20, 14, 14, 22, 32, 10])
    notes = ["HR-MAINTAINED SHEET (yellow = editable).",
             "Add/edit shifts here: Start/End as hh:mm AM/PM. End earlier than or equal to Start = overnight shift.",
             "Tolerances are minutes (0-240). Active = Yes/No. Removing a row deactivates the shift.",
             "Save the file, then upload it in the Admin Portal (or save in place); changes are validated and applied.",
             "No shifts are built in: employees whose Shift is not listed here are flagged, never guessed."]
    for i, t in enumerate(notes, start=1):
        ws.cell(i, 8, t).font = Font(italic=True, size=9)


def _sheet_employee_master(ws, emps, shifts):
    head = hr_import.EMP_HEADERS + ["Registered", "Shift Check"]
    ws.append(head)
    for e in emps:
        _, issue = sh.resolve_shift(e, shifts)
        ws.append([e.employee_id, e.employee_name, e.verified_email, e.department or "", e.designation or "",
                   e.location or "", e.assigned_shift or "", e.account_status, "Yes" if e.registered_at else "No",
                   "OK" if not issue else issue])
    for row in ws.iter_rows(min_row=2):
        for c in row[:8]:
            c.fill = HR_FILL
        if row[9].value != "OK":
            row[9].fill = BAD_FILL
    _style_table(ws, len(head), [12, 26, 32, 16, 18, 14, 16, 18, 11, 34])
    ws.cell(1, 12, "Editable: name, department, designation, location, Shift, Account Status (ACTIVE/INACTIVE). "
                   "Verified e-mail of registered employees cannot be changed here.").font = Font(italic=True, size=9)


def _style_table(ws, ncols, widths):
    for c in range(1, ncols + 1):
        cell = ws.cell(1, c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HDR_FILL
        cell.alignment = CENTER
        ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{ws.max_row}"


_gen_lock = threading.Lock()


def _state_file(path: Path) -> Path:
    return path.with_suffix(".hrstate")


def _sync_hr_edits(db: Session, settings: Settings, path: Path) -> dict | None:
    """If HR edited Shift Master / Employee Master in the file in place, apply them first."""
    sf = _state_file(path)
    if not path.exists() or not sf.exists():
        return None
    cur, last = hr_import.sheets_fingerprint(path), sf.read_text().strip()
    if cur is None or cur == last:
        return None
    try:
        res = hr_import.import_workbook(db, settings, path, actor="hr-workbook-sync")
        return {"applied": res}
    except hr_import.HRImportError as exc:
        db.rollback()
        kept = path.with_name(f"attendance.rejected-{datetime.now():%Y%m%d-%H%M%S}.xlsx")
        shutil.copy2(path, kept)
        audit(db, "hr-workbook-sync", "HR_SHEETS_REJECTED", "workbook", kept.name, {"errors": exc.errors[:30]})
        db.commit()
        return {"rejected": exc.errors, "preserved_as": kept.name}
    except Exception as exc:  # noqa: BLE001 - corrupt file etc.
        db.rollback()
        kept = path.with_name(f"attendance.rejected-{datetime.now():%Y%m%d-%H%M%S}.xlsx")
        shutil.copy2(path, kept)
        audit(db, "hr-workbook-sync", "HR_SHEETS_REJECTED", "workbook", kept.name, {"errors": [f"unreadable: {exc}"]})
        db.commit()
        return {"rejected": [f"unreadable workbook: {exc}"], "preserved_as": kept.name}


def generate_report(db: Session, settings: Settings | None = None) -> Path:
    """Regenerate the workbook atomically (temp file + replace) under a cross-process file lock.
    HR edits to the two HR sheets are applied to the database first, then everything is rebuilt."""
    s = settings or get_settings()
    path = report_path(s)
    with _gen_lock, FileLock(str(path) + ".lock", timeout=60):
        status["hr_sync"] = _sync_hr_edits(db, s, path)
        wb = build_workbook(db, s)
        tmp = path.with_suffix(".tmp.xlsx")
        wb.save(tmp)
        for attempt in range(4):                    # Windows: brief locks (antivirus/indexer) clear in a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                _clock.sleep(0.4)
        fp = hr_import.sheets_fingerprint(path)
        if fp:
            _state_file(path).write_text(fp)
        status["generated_at"], status["error"] = datetime.now().isoformat(timespec="seconds"), None
    return path


class ReportWorker:
    """Single background worker; many punch events coalesce into one regeneration."""

    def __init__(self, session_factory=None):
        self._factory = session_factory
        self._evt = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.runs = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="report-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self._evt.set()

    def request_update(self):
        self._evt.set()

    def _loop(self):
        while not self._stop:
            self._evt.wait()
            self._evt.clear()
            if self._stop:
                break
            self.run_once()

    def run_once(self):
        from app.database.connection import SessionLocal

        factory = self._factory or SessionLocal
        try:
            with factory() as db:
                generate_report(db)
            self.last_error = None
            self.runs += 1
        except Exception as exc:  # reliable: failure is recorded and retried automatically
            locked = isinstance(exc, PermissionError)
            msg = ("attendance.xlsx is open in Excel (or another program) and cannot be replaced - close it; "
                   "the report will refresh automatically within a minute") if locked else str(exc)
            self.last_error = msg
            status["error"] = f"{datetime.now().isoformat(timespec='seconds')}: {msg}"
            if locked:
                log.warning("Excel regeneration postponed: %s", msg)   # expected & self-healing: no stack trace
            else:
                log.exception("Excel regeneration failed")
            if not self._stop:                                          # retry later until it succeeds
                t = threading.Timer(RETRY_SECONDS, self.request_update)
                t.daemon = True
                t.start()
            try:
                with factory() as db:
                    audit(db, "report-worker", "REPORT_ERROR", "report", WORKBOOK_NAME, {"error": str(exc)[:500]})
                    db.commit()
            except Exception:  # noqa: BLE001
                log.exception("could not audit report error")


worker = ReportWorker()
