import io
from datetime import date, datetime, time

from openpyxl import load_workbook
from sqlalchemy import select

from app.database.models import AttendanceEvent, Employee, ShiftMaster
from app.services import attendance_service as att
from app.services import hr_import
from app.services.excel_generator import build_workbook, generate_report, report_path
from conftest import DOMAIN, add_master, add_shift, ist, login, punch, register


def D(v):
    return v.date() if isinstance(v, datetime) else v


def ev(db, emp, typ, ts, source="QR"):
    db.add(AttendanceEvent(employee_id=emp.id, event_type=typ, event_timestamp=ts, source=source,
                           network_verified=True, recorded_by="t"))


def test_daily_sheet_layout_shift_overnight_status_raw(db, settings, freeze):
    freeze(2026, 11, 6, 12, 0)
    add_shift(db, "General")
    add_master(db, settings, "E1", "Ajith Kumar", "ajith@gwcdata.ai", shift="General")
    add_master(db, settings, "E2", "Abinaya Sindhu", "abi@gwcdata.ai", shift="Nope")
    a, b = (db.scalar(select(Employee).where(Employee.employee_id == i)) for i in ("E1", "E2"))
    ev(db, a, "IN", ist(2026, 11, 3, 13, 45))
    ev(db, a, "OUT", ist(2026, 11, 4, 0, 53))          # overnight
    ev(db, a, "IN", ist(2026, 11, 4, 13, 30))          # open on the 4th
    db.commit()
    wb = build_workbook(db, settings)
    assert wb.sheetnames == ["Daily Attendance", "Raw Punch Logs", "Monthly Summary", "Shift Master", "Employee Master"]
    ws = wb["Daily Attendance"]
    assert [ws.cell(1, c).value for c in (1, 2, 3)] == ["S.No", "Employee Name", "Shift"]
    merged = {str(r) for r in ws.merged_cells.ranges}
    assert {"D1:E1", "F1:G1"} <= merged
    assert ws["D1"].value == date(2026, 11, 3) and ws["D1"].number_format == "dd-mmm-yyyy"
    assert [ws.cell(2, c).value for c in (4, 5, 6, 7)] == ["In Time", "Out Time", "In Time", "Out Time"]
    assert ws["B3"].value == "Abinaya Sindhu" and ws["C3"].value == "Nope (INVALID)" and ws["D3"].value is None   # no punches -> blank (no status feature)
    assert ws["B4"].value == "Ajith Kumar" and ws["C4"].value == "General"           # Shift column retained
    assert ws["D4"].value == time(13, 45) and ws["D4"].number_format == "hh:mm AM/PM"
    assert ws["E4"].value == time(0, 53)                                              # overnight OUT under IN's date
    assert ws["F4"].value == time(13, 30) and ws["G4"].value is None                  # open session stays blank
    assert wb["Raw Punch Logs"].max_row == 4


def test_punch_updates_correct_employee_and_date_cells(shifted, db, settings, freeze):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 5)
    punch(c, csrf, "IN")
    generate_report(db, settings)
    ws = load_workbook(report_path(settings))["Daily Attendance"]
    col = next(i for i in range(4, ws.max_column) if D(ws.cell(1, i).value) == date(2026, 11, 4))
    assert ws.cell(3, 2).value == "John" and ws.cell(3, col).value == time(9, 5) and ws.cell(3, col + 1).value is None
    freeze(2026, 11, 4, 18, 2)
    punch(c, csrf, "OUT")
    generate_report(db, settings)
    ws = load_workbook(report_path(settings))["Daily Attendance"]
    assert ws.cell(3, col).value == time(9, 5) and ws.cell(3, col + 1).value == time(18, 2)


def test_history_preserved_and_multiple_sessions_policy(db, settings, freeze):
    freeze(2026, 11, 6, 12, 0)
    add_master(db, settings, "E1", "A", "a@gwcdata.ai")
    a = db.scalar(select(Employee))
    for t, h, m in (("IN", 9, 0), ("OUT", 13, 0), ("IN", 14, 0), ("OUT", 18, 30)):
        ev(db, a, t, ist(2026, 11, 3, h, m))
    ev(db, a, "IN", ist(2026, 11, 4, 9, 0))
    ev(db, a, "OUT", ist(2026, 11, 4, 17, 0))
    db.commit()
    generate_report(db, settings)
    ws = load_workbook(report_path(settings))["Daily Attendance"]
    cols = {D(ws.cell(1, c).value): c for c in range(4, ws.max_column + 1, 2) if ws.cell(1, c).value}
    c3, c4 = cols[date(2026, 11, 3)], cols[date(2026, 11, 4)]
    assert (ws.cell(3, c3).value, ws.cell(3, c3 + 1).value) == (time(9, 0), time(18, 30))   # first IN / last OUT
    assert (ws.cell(3, c4).value, ws.cell(3, c4 + 1).value) == (time(9, 0), time(17, 0))    # earlier day untouched
    assert load_workbook(report_path(settings))["Raw Punch Logs"].max_row == 7             # all 6 raw events


def test_monthly_summary_is_accurate(db, settings, freeze):
    freeze(2026, 11, 6, 12, 0)                       # Friday
    add_shift(db)
    add_master(db, settings, "E1", "A", "a@gwcdata.ai", shift="General")
    a = db.scalar(select(Employee))
    ev(db, a, "IN", ist(2026, 11, 2, 9, 0)); ev(db, a, "OUT", ist(2026, 11, 2, 18, 0))      # 9h on time
    ev(db, a, "IN", ist(2026, 11, 3, 9, 45)); ev(db, a, "OUT", ist(2026, 11, 3, 16, 30))    # 6.75h late+early
    ev(db, a, "IN", ist(2026, 11, 4, 9, 0))                                                 # never closed
    db.commit()                                                                              # Nov 5 absent
    ms = build_workbook(db, settings)["Monthly Summary"]
    row = dict(zip([c.value for c in ms[1]], [c.value for c in ms[2]]))
    assert row["Shift"] == "General" and row["Working Days (to date)"] == 5 and row["Days Present"] == 3
    assert row["Absent"] == 1 and row["Total Hours"] == 15.75
    assert (row["Late Arrivals"], row["Early Departures"], row["Missing Punch Out"]) == (1, 1, 1)
    assert row["Attendance Status"] == "Review" and "OUT missing 04-Nov" in row["Missing-Punch Indicator"]


def hr_sheets(db, settings, mutate):
    wb = build_workbook(db, settings)
    mutate(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_shift_master_sheet_is_empty_not_pre_filled(db, settings):
    ws = build_workbook(db, settings)["Shift Master"]
    assert [c.value for c in ws[1]][:5] == ["Shift Name", "Start Time", "End Time", "Late Tolerance Minutes",
                                           "Early Departure Tolerance Minutes"]
    assert all(ws.cell(r, 1).value is None for r in range(2, ws.max_row + 1))   # no sample shifts assumed as policy


def test_hr_upload_creates_shifts_and_assigns_employees(db, settings):
    add_master(db, settings, "E1", "A", "a@gwcdata.ai")

    def edit(wb):
        s = wb["Shift Master"]
        s.append(["General", "09:00 AM", "06:00 PM", 10, 10, "Yes"])
        s.append(["Night", time(22, 0), time(6, 0), 15, 20, "Yes"])
        wb["Employee Master"].cell(2, 7).value = "night"            # case-insensitive mapping
    res = hr_import.import_workbook(db, settings, hr_sheets(db, settings, edit), "hr@gwcdata.ai")
    assert res["shifts_created"] == 2 and res["employees_updated"] == 1
    night = db.scalar(select(ShiftMaster).where(ShiftMaster.shift_name == "Night"))
    assert (night.start_time, night.end_time, night.late_tolerance_minutes, night.early_tolerance_minutes) == \
        (time(22, 0), time(6, 0), 15, 20)
    assert db.scalar(select(Employee)).assigned_shift == "night"


def test_hr_updates_are_preserved_on_regeneration(db, settings):
    add_shift(db, "General")
    add_master(db, settings, "E1", "A", "a@gwcdata.ai", shift="General")

    def edit(wb):
        s = wb["Shift Master"]
        s.cell(2, 2).value, s.cell(2, 4).value = time(8, 0), 25
        s.append(["Evening", "02:00 PM", "10:00 PM", 5, 5, "Yes"])
        wb["Employee Master"].cell(2, 7).value = "Evening"
    hr_import.import_workbook(db, settings, hr_sheets(db, settings, edit), "hr@gwcdata.ai")
    generate_report(db, settings)
    wb = load_workbook(report_path(settings))
    rows = {r[0].value: [c.value for c in r] for r in wb["Shift Master"].iter_rows(min_row=2)}
    assert rows["General"][1] == time(8, 0) and rows["General"][3] == 25 and rows["Evening"][4] == 5
    assert wb["Employee Master"]["G2"].value == "Evening" and wb["Daily Attendance"]["C3"].value == "Evening"
    generate_report(db, settings)                                      # again: still stable
    assert load_workbook(report_path(settings))["Employee Master"]["G2"].value == "Evening"


def test_in_place_edit_of_hr_sheets_is_applied_not_overwritten(db, settings):
    add_shift(db, "General")
    add_master(db, settings, "E1", "A", "a@gwcdata.ai", shift="General")
    p = generate_report(db, settings)
    wb = load_workbook(p)
    wb["Shift Master"].cell(2, 4).value = 45                           # HR edits the file in place
    wb["Employee Master"].cell(2, 4).value = "Finance"
    wb.save(p)
    generate_report(db, settings)                                      # e.g. triggered by the next punch
    assert db.scalar(select(ShiftMaster)).late_tolerance_minutes == 45
    db.expire_all()
    assert db.scalar(select(Employee)).department == "Finance"
    assert load_workbook(p)["Shift Master"].cell(2, 4).value == 45


def test_invalid_hr_edit_is_rejected_whole_and_preserved(db, settings):
    add_shift(db, "General")
    add_master(db, settings, "E1", "A", "a@gwcdata.ai", shift="General")
    p = generate_report(db, settings)
    wb = load_workbook(p)
    wb["Shift Master"].cell(2, 2).value = "not a time"
    wb["Shift Master"].cell(2, 4).value = 99
    wb.save(p)
    from app.services import excel_generator
    generate_report(db, settings)
    assert db.scalar(select(ShiftMaster)).late_tolerance_minutes == 10          # DB unchanged
    sync = excel_generator.status["hr_sync"]
    assert sync["rejected"] and sync["preserved_as"].startswith("attendance.rejected-")
    assert (p.parent / sync["preserved_as"]).exists()                           # HR's file is not lost
    assert load_workbook(p)["Shift Master"].cell(2, 4).value == 10             # workbook regenerated from DB


def test_hr_import_validation_errors(db, settings):
    add_master(db, settings, "E1", "A", "a@gwcdata.ai")

    def bad(wb):
        s = wb["Shift Master"]
        s.append(["X", "09:00 AM", "05:00 PM", 10, 10, "Yes"])
        s.append(["Z", "09:00 AM", "09:00 AM", 10, 10, "Yes"])             # start == end
        s.append(["Y", "09:00 AM", "05:00 PM", 500, 10, "Yes"])            # tolerance out of range
        s.append(["x", "09:00 AM", "05:00 PM", 10, 10, "Yes"])             # duplicate (case-insensitive)
        wb["Employee Master"].cell(2, 3).value = "a@gmail.com"            # wrong domain
    try:
        hr_import.import_workbook(db, settings, hr_sheets(db, settings, bad), "hr@gwcdata.ai")
        raise AssertionError("expected rejection")
    except hr_import.HRImportError as e:
        assert len(e.errors) == 4
    assert db.query(ShiftMaster).count() == 0                                   # nothing partially applied


def test_registered_email_cannot_be_changed_by_hr_sheet(client, db, settings):
    add_master(db, settings, "EMP001", "John", f"john.jack@{DOMAIN}")
    register(client, f"john.jack@{DOMAIN}", "EMP001")

    def edit(wb):
        wb["Employee Master"].cell(2, 3).value = f"other@{DOMAIN}"
    try:
        hr_import.import_workbook(db, settings, hr_sheets(db, settings, edit), "hr@gwcdata.ai")
        raise AssertionError("expected rejection")
    except hr_import.HRImportError as e:
        assert "cannot be changed" in e.errors[0]


def test_late_detection_uses_shift_master_values_after_hr_update(shifted, db, settings, freeze):
    c, csrf = shifted

    def edit(wb):
        wb["Shift Master"].cell(2, 4).value = 60                      # HR relaxes tolerance to 60 min
    hr_import.import_workbook(db, settings, hr_sheets(db, settings, edit), "hr@gwcdata.ai")
    freeze(2026, 11, 4, 9, 45)
    assert punch(c, csrf, "IN").json()["remark_required"] is None


def test_report_error_is_recorded(db, settings, monkeypatch):
    from app.database.models import AuditLog
    from app.services import excel_generator as eg
    monkeypatch.setattr(eg, "build_workbook", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    w = eg.ReportWorker(session_factory=lambda: _Ctx(db))
    w.run_once()
    assert "disk full" in (w.last_error or "") and "disk full" in eg.status["error"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "REPORT_ERROR")) is not None


class _Ctx:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


def test_workbook_only_for_admins(make_client, db, settings):
    add_master(db, settings)
    emp = make_client()
    login(emp, f"john.jack@{DOMAIN}")
    assert emp.get("/admin/api/report/download").status_code == 403
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    r = adm.get("/admin/api/report/download")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    for p in ("/static/attendance.xlsx", "/reports/attendance.xlsx", "/storage/reports/attendance.xlsx"):
        assert make_client().get(p).status_code == 404


def test_sessions_helper_sanity(db, settings):
    assert att.fmt_duration(3 * 3600 + 5 * 60) == "3h 05m"


def test_locked_workbook_is_reported_clearly_and_retried(db, settings, monkeypatch):
    import os
    from app.services import excel_generator as eg
    add_master(db, settings)
    scheduled = []

    class FakeTimer:
        def __init__(self, secs, fn):
            scheduled.append((secs, fn))
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(eg.threading, "Timer", FakeTimer)
    monkeypatch.setattr(eg._clock, "sleep", lambda s: None)
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError(5, "Access is denied")))
    w = eg.ReportWorker(session_factory=lambda: _Ctx(db))
    w.run_once()
    assert "open in Excel" in w.last_error and "open in Excel" in eg.status["error"]
    assert scheduled and scheduled[0][0] == eg.RETRY_SECONDS             # will retry by itself
    monkeypatch.undo()
    w.run_once()                                                          # Excel closed -> succeeds
    assert w.last_error is None and eg.status["error"] is None
