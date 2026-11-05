from datetime import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import models
from app.database.models import AttendanceEvent, AttendanceRemark, AuditLog
from conftest import DOMAIN, add_master, add_shift, punch, register

H = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731


def test_late_arrival_triggers_remark_request(shifted, freeze, db):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 45)
    r = punch(c, csrf, "IN").json()
    rr = r["remark_required"]
    assert rr["title"] == "Late Arrival"
    assert rr["message"] == "You punched in at 09:45 AM. Your scheduled shift starts at 09:00 AM."
    assert rr["detail"] == "Delay: 45 minutes." and rr["remark_status"] == "Pending Employee Remark"
    assert rr["reasons"] == ["Traffic / Transportation delay", "Personal emergency", "Health-related reason",
                             "Work-related reason", "Other"]
    row = db.scalar(select(AttendanceRemark))
    ev = db.scalar(select(AttendanceEvent))
    assert row.attendance_id == ev.attendance_id and row.remark_status == "Pending Employee Remark"
    # the original punch time is untouched and also visible on the page state
    assert r["attendance"]["punch_in"] == "09:45 AM" and r["attendance"]["pending_remarks"][0]["remark_id"] == row.remark_id


def test_early_punch_out_triggers_remark_request(shifted, freeze, db):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 0)
    assert punch(c, csrf, "IN").json()["remark_required"] is None
    freeze(2026, 11, 4, 16, 30)
    rr = punch(c, csrf, "OUT").json()["remark_required"]
    assert rr["title"] == "Early Punch Out"
    assert rr["message"] == "You punched out at 04:30 PM. Your scheduled shift ends at 06:00 PM."
    assert rr["detail"] == "Early departure: 1 hour 30 minutes."
    assert rr["reasons"][2] == "Manager-approved early departure"
    assert db.scalar(select(AttendanceRemark).where(AttendanceRemark.exception_type == "EARLY_DEPARTURE")) is not None


@pytest.mark.parametrize("in_at,out_at", [((9, 0), (18, 0)), ((9, 10), (17, 50)), ((8, 30), (19, 0))])
def test_punches_within_tolerance_need_no_remark(shifted, freeze, db, in_at, out_at):
    c, csrf = shifted
    freeze(2026, 11, 4, *in_at)
    assert punch(c, csrf, "IN").json()["remark_required"] is None
    freeze(2026, 11, 4, *out_at)
    assert punch(c, csrf, "OUT").json()["remark_required"] is None
    assert db.query(AttendanceRemark).count() == 0


def test_one_minute_beyond_tolerance_requires_remark(shifted, freeze):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 11)
    assert punch(c, csrf, "IN").json()["remark_required"]["variance_minutes"] == 11


def test_only_first_in_of_day_can_be_late(shifted, freeze, db):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 0)
    punch(c, csrf, "IN")
    freeze(2026, 11, 4, 12, 0)
    punch(c, csrf, "OUT")                       # early departure (before 18:00) -> remark, by policy
    freeze(2026, 11, 4, 13, 0)
    assert punch(c, csrf, "IN").json()["remark_required"] is None   # coming back from a break is not "late"
    assert db.query(AttendanceRemark).filter_by(exception_type="LATE_ARRIVAL").count() == 0


def test_tolerance_comes_from_shift_master(shifted, freeze, db):
    c, csrf = shifted
    db.query(models.ShiftMaster).update({"late_tolerance_minutes": 60})
    db.commit()
    freeze(2026, 11, 4, 9, 45)
    assert punch(c, csrf, "IN").json()["remark_required"] is None   # 45 min is within the HR-set 60 min tolerance


def test_no_valid_shift_means_no_remark_and_flags_issue(registered, freeze, db):
    c, csrf = registered                         # no shift assigned
    freeze(2026, 11, 4, 14, 0)
    r = punch(c, csrf, "IN").json()
    assert r["remark_required"] is None and r["attendance"]["shift"] is None
    assert r["attendance"]["shift_issue"] == "NO_SHIFT_ASSIGNED"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "SHIFT_CONFIG_ISSUE")) is not None


def test_unknown_or_inactive_shift_is_flagged_not_assumed(client, db, settings, freeze):
    add_shift(db, "Retired", active=False)
    add_master(db, settings, shift="Retired")
    csrf = register(client, f"john.jack@{DOMAIN}", "EMP001")
    freeze(2026, 11, 4, 14, 0)
    r = punch(client, csrf, "IN").json()
    assert r["remark_required"] is None and r["attendance"]["shift_issue"].startswith("UNKNOWN_OR_INACTIVE_SHIFT")


def test_submit_changes_status_and_is_final(shifted, freeze, db):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 45)
    rid = punch(c, csrf, "IN").json()["remark_required"]["remark_id"]
    bad = c.post(f"/api/remarks/{rid}/submit", json={"reason_category": "Because", "remark": "x"}, headers=H(csrf))
    assert bad.status_code == 422
    r = c.post(f"/api/remarks/{rid}/submit", headers=H(csrf), json={
        "reason_category": "Traffic / Transportation delay", "remark": "Traffic was unusually heavy due to road construction."})
    assert r.status_code == 200 and r.json()["remark"]["remark_status"] == "Remark Submitted"
    row = db.scalar(select(AttendanceRemark))
    db.refresh(row)
    assert row.remark_status == "Remark Submitted" and row.submitted_at is not None
    again = c.post(f"/api/remarks/{rid}/submit", json={"reason_category": "Other"}, headers=H(csrf))
    assert again.status_code == 409                                  # cannot be rewritten
    mine = c.get("/api/remarks").json()["remarks"]
    assert mine[0]["remark_status"] == "Remark Submitted" and "road construction" in mine[0]["employee_remark"]
    assert c.get("/api/attendance/today").json()["pending_remarks"] == []
    assert db.query(AttendanceEvent).count() == 1                    # raw event preserved, not touched


def test_cannot_submit_or_read_someone_elses_remark(shifted, make_client, freeze, db, settings):
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 45)
    rid = punch(c, csrf, "IN").json()["remark_required"]["remark_id"]
    add_master(db, settings, "EMP002", "Jane", f"jane@{DOMAIN}", shift="General")
    other = make_client()
    csrf2 = register(other, f"jane@{DOMAIN}", "EMP002")
    assert other.post(f"/api/remarks/{rid}/submit", json={"reason_category": "Other"}, headers=H(csrf2)).status_code == 404
    assert other.get("/api/remarks").json()["remarks"] == []


def test_exactly_two_remark_statuses_enforced_by_database(shifted, freeze, db):
    assert models.REMARK_STATUSES == ("Pending Employee Remark", "Remark Submitted")
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 45)
    punch(c, csrf, "IN")
    for bad in ("Resolved", "HR Approved", "Pending HR Review", "AI Investigation Completed", "HR Rejected"):
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE attendance_remarks SET remark_status = :s"), {"s": bad})
            db.commit()
        db.rollback()
    with pytest.raises(IntegrityError):           # 'submitted' without a reason/timestamp is also rejected
        db.execute(text("UPDATE attendance_remarks SET remark_status = 'Remark Submitted'"))
        db.commit()
    db.rollback()


def test_overnight_shift_remarks(client, db, settings, freeze):
    add_shift(db, "Night", time(22, 0), time(6, 0))
    add_master(db, settings, shift="Night")
    csrf = register(client, f"john.jack@{DOMAIN}", "EMP001")
    freeze(2026, 11, 4, 22, 5)
    assert punch(client, csrf, "IN").json()["remark_required"] is None
    freeze(2026, 11, 5, 4, 0)
    rr = punch(client, csrf, "OUT").json()["remark_required"]
    assert rr["title"] == "Early Punch Out" and rr["variance_minutes"] == 120
    assert rr["scheduled_time"] == "06:00 AM" and rr["work_date"] == "2026-11-05"


def test_overnight_exact_end_has_no_remark(client, db, settings, freeze):
    add_shift(db, "Night", time(22, 0), time(6, 0))
    add_master(db, settings, shift="Night")
    csrf = register(client, f"john.jack@{DOMAIN}", "EMP001")
    freeze(2026, 11, 4, 22, 0)
    punch(client, csrf, "IN")
    freeze(2026, 11, 5, 6, 0)
    assert punch(client, csrf, "OUT").json()["remark_required"] is None
    t = client.get("/api/attendance/today").json()
    assert t["punch_in"] == "10:00 PM" and t["punch_out"] == "06:00 AM"   # overnight session still paired
