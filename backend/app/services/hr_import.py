"""HR-maintained sheets: 'Shift Master' and 'Employee Master' inside the workbook.

HR edits these two sheets in Excel and either uploads the file in the Admin Portal or simply saves the
file in place; the next regeneration detects the change, validates it and applies it to the database
(source of truth) BEFORE rebuilding, so HR values are never overwritten. Invalid edits are rejected as a
whole (nothing partially applied) and the HR file is preserved under a *.rejected-<timestamp>.xlsx name.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, time
from pathlib import Path
from typing import BinaryIO

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import Employee, ShiftMaster
from app.services.audit import audit
from app.services.employee_service import EMP_ID_RE, email_in_domain, normalise_email

SHIFT_SHEET = "Shift Master"
EMP_SHEET = "Employee Master"
SHIFT_HEADERS = ["Shift Name", "Start Time", "End Time", "Late Tolerance Minutes",
                 "Early Departure Tolerance Minutes", "Active"]
EMP_HEADERS = ["Employee ID", "Employee Name", "Email", "Department", "Designation", "Location", "Shift",
               "Account Status"]
HR_STATUS = {"ACTIVE", "INACTIVE"}


class HRImportError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def parse_time(v) -> time:
    """Accepts Excel time cells, datetimes, 'hh:mm AM/PM' and 'HH:MM' strings, or day-fractions."""
    if isinstance(v, datetime):
        return v.time().replace(second=0, microsecond=0)
    if isinstance(v, time):
        return v.replace(second=0, microsecond=0)
    if isinstance(v, (int, float)) and 0 <= v < 1:
        mins = round(v * 24 * 60)
        return time(mins // 60 % 24, mins % 60)
    if isinstance(v, str):
        s = v.strip().upper().replace(".", "")
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%I %p"):
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                pass
    raise ValueError(f"unrecognised time {v!r} (use e.g. 09:00 AM)")


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def _rows(ws, headers: list[str], sheet: str, errors: list[str]) -> list[tuple[int, dict]]:
    first = [(_cell(c.value)) for c in ws[1]]
    idx: dict[str, int] = {}
    for h in headers:
        match = [i for i, x in enumerate(first) if x.lower() == h.lower()]
        if not match:
            if h in ("Active", "Account Status", "Department", "Designation", "Location"):
                continue  # optional columns
            errors.append(f"{sheet}: missing column '{h}'")
        else:
            idx[h] = match[0]
    out = []
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(c not in (None, "") for c in row[:len(headers)]):
            continue
        out.append((n, {h: (row[i] if i < len(row) else None) for h, i in idx.items()}))
    return out


def read_hr_sheets(source: str | Path | BinaryIO) -> dict:
    wb = load_workbook(source, data_only=True, read_only=False)
    errors: list[str] = []
    for name in (SHIFT_SHEET, EMP_SHEET):
        if name not in wb.sheetnames:
            errors.append(f"workbook has no '{name}' sheet")
    if errors:
        raise HRImportError(errors)
    return {"shifts": _rows(wb[SHIFT_SHEET], SHIFT_HEADERS, SHIFT_SHEET, errors),
            "employees": _rows(wb[EMP_SHEET], EMP_HEADERS, EMP_SHEET, errors), "errors": errors}


def sheets_fingerprint(path: Path) -> str | None:
    """Hash of the HR-editable content only (values as stored in the file)."""
    try:
        wb = load_workbook(path, data_only=True)
        h = hashlib.sha256()
        for name, ncols in ((SHIFT_SHEET, len(SHIFT_HEADERS)), (EMP_SHEET, len(EMP_HEADERS))):
            h.update(name.encode())
            if name not in wb.sheetnames:
                h.update(b"<missing>")
                continue
            for row in wb[name].iter_rows(min_row=1, max_col=ncols, values_only=True):
                if any(c not in (None, "") for c in row):
                    h.update(repr(tuple(_cell(c) for c in row)).encode())
        return h.hexdigest()
    except Exception:  # noqa: BLE001 - unreadable file is handled by the caller
        return None


def apply_hr_sheets(db: Session, settings: Settings, parsed: dict, actor: str) -> dict:
    errors: list[str] = list(parsed["errors"])
    shifts: dict[str, dict] = {}
    for n, r in parsed["shifts"]:
        name = _cell(r.get("Shift Name"))
        try:
            if not name:
                raise ValueError("Shift Name is required")
            if name.lower() in shifts:
                raise ValueError(f"duplicate shift name '{name}'")
            start, end = parse_time(r.get("Start Time")), parse_time(r.get("End Time"))
            if start == end:
                raise ValueError("start and end time must differ")
            tols = []
            for col in ("Late Tolerance Minutes", "Early Departure Tolerance Minutes"):
                v = r.get(col)
                if isinstance(v, str):
                    v = v.strip()
                tol = int(float(v))  # blank/None/text raise ValueError/TypeError -> reported below
                if not 0 <= tol <= 240:
                    raise ValueError(f"{col} must be 0-240")
                tols.append(tol)
            active_raw = _cell(r.get("Active")).lower()
            if active_raw not in ("", "yes", "no", "true", "false", "1", "0", "y", "n"):
                raise ValueError("Active must be Yes or No")
            shifts[name.lower()] = {"name": name, "start": start, "end": end, "late": tols[0], "early": tols[1],
                                    "active": active_raw in ("", "yes", "true", "1", "y")}
        except (ValueError, TypeError) as exc:
            errors.append(f"{SHIFT_SHEET} row {n}: {exc}")
    emps: list[dict] = []
    seen_ids: set[str] = set()
    for n, r in parsed["employees"]:
        eid = _cell(r.get("Employee ID")).upper()
        try:
            if not EMP_ID_RE.match(eid):
                raise ValueError("invalid Employee ID")
            if eid in seen_ids:
                raise ValueError(f"duplicate Employee ID {eid}")
            seen_ids.add(eid)
            email = normalise_email(_cell(r.get("Email")))
            if not email_in_domain(email, settings):
                raise ValueError(f"email must be @{settings.allowed_email_domain}")
            if not _cell(r.get("Employee Name")):
                raise ValueError("Employee Name is required")
            status = _cell(r.get("Account Status")).upper() or None
            if status and status not in HR_STATUS | {"PENDING_APPROVAL"}:
                raise ValueError("Account Status must be ACTIVE or INACTIVE")
            emps.append({"id": eid, "name": _cell(r.get("Employee Name")), "email": email,
                         "dept": _cell(r.get("Department")) or None, "desig": _cell(r.get("Designation")) or None,
                         "loc": _cell(r.get("Location")) or None, "shift": _cell(r.get("Shift")) or None,
                         "status": status, "row": n})
        except ValueError as exc:
            errors.append(f"{EMP_SHEET} row {n}: {exc}")
    if errors:
        raise HRImportError(errors)

    # ---- apply (single transaction; caller commits) ----
    changes = {"shifts_created": 0, "shifts_updated": 0, "shifts_deactivated": 0,
               "employees_created": 0, "employees_updated": 0, "warnings": []}
    existing = {s.shift_name.lower(): s for s in db.scalars(select(ShiftMaster))}
    for key, s in shifts.items():
        row = existing.get(key)
        if row is None:
            db.add(ShiftMaster(shift_name=s["name"], start_time=s["start"], end_time=s["end"],
                               late_tolerance_minutes=s["late"], early_tolerance_minutes=s["early"], active=s["active"]))
            changes["shifts_created"] += 1
        else:
            new = (s["start"], s["end"], s["late"], s["early"], s["active"])
            old = (row.start_time, row.end_time, row.late_tolerance_minutes, row.early_tolerance_minutes, row.active)
            if new != old:
                row.start_time, row.end_time, row.late_tolerance_minutes = s["start"], s["end"], s["late"]
                row.early_tolerance_minutes, row.active = s["early"], s["active"]
                changes["shifts_updated"] += 1
    for key, row in existing.items():  # a shift deleted from the sheet is deactivated, never hard-deleted
        if key not in shifts and row.active:
            row.active = False
            changes["shifts_deactivated"] += 1
    known = {s["name"].lower() for s in shifts.values() if s["active"]}
    for e in emps:
        emp = db.scalar(select(Employee).where(Employee.employee_id == e["id"]))
        if e["shift"] and e["shift"].lower() not in known:
            changes["warnings"].append(f"{e['id']}: shift '{e['shift']}' is not an active Shift Master entry "
                                       "(flagged; no schedule will be assumed)")
        if emp is None:
            if db.scalar(select(Employee).where(Employee.verified_email == e["email"])):
                raise HRImportError([f"{EMP_SHEET} row {e['row']}: email already belongs to another employee"])
            db.add(Employee(employee_id=e["id"], employee_name=e["name"], verified_email=e["email"],
                            department=e["dept"], designation=e["desig"], location=e["loc"],
                            assigned_shift=e["shift"],
                            account_status=e["status"] if e["status"] in HR_STATUS else "ACTIVE"))
            changes["employees_created"] += 1
            continue
        if emp.verified_email != e["email"]:
            if emp.registered_at:
                raise HRImportError([f"{EMP_SHEET} row {e['row']}: verified e-mail of a registered employee "
                                     "cannot be changed here"])
            if db.scalar(select(Employee).where(Employee.verified_email == e["email"])):
                raise HRImportError([f"{EMP_SHEET} row {e['row']}: email already belongs to another employee"])
            emp.verified_email = e["email"]
        before = (emp.employee_name, emp.department, emp.designation, emp.location, emp.assigned_shift,
                  emp.account_status)
        emp.employee_name, emp.department, emp.designation, emp.location = e["name"], e["dept"], e["desig"], e["loc"]
        emp.assigned_shift = e["shift"]
        if e["status"] in HR_STATUS:
            emp.account_status = e["status"]
        if before != (emp.employee_name, emp.department, emp.designation, emp.location, emp.assigned_shift,
                      emp.account_status):
            changes["employees_updated"] += 1
    audit(db, actor, "HR_SHEETS_IMPORTED", "workbook", None, {k: v for k, v in changes.items() if k != "warnings"}
          | {"warnings": changes["warnings"][:20]})
    return changes


def import_workbook(db: Session, settings: Settings, source, actor: str) -> dict:
    parsed = read_hr_sheets(source)
    res = apply_hr_sheets(db, settings, parsed, actor)
    db.commit()
    return res

