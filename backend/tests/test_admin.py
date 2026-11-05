from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app.database.models import AdminUser, AttendanceEvent, AuditLog, Employee
from conftest import DOMAIN, add_master, add_shift, login, punch

H = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731
ADMIN_GETS = ("/admin/api/me", "/admin/api/dashboard", "/admin/api/attendance", "/admin/api/monthly",
              "/admin/api/remarks", "/admin/api/employees", "/admin/api/report/download",
              "/admin/api/agent/status", "/admin/api/agent/monitor/latest")
REMOVED = ("/admin/api/cases", "/admin/api/corrections", "/admin/api/audit", "/admin/api/shifts",
           "/admin/api/notifications", "/admin/api/report/status")


def test_admin_endpoints_deny_anonymous_and_employees(registered, make_client):
    c, csrf = registered
    for path in ADMIN_GETS:
        assert c.get(path).status_code == 403, path                       # employee session
        assert make_client().get(path).status_code == 401, path           # anonymous
    for p in ("/admin/api/agent/daily-summary", "/admin/api/agent/monitor/run"):
        assert c.post(p, headers=H(csrf)).status_code == 403
    assert c.post("/admin/api/employees/EMP001/status", json={"status": "INACTIVE"}, headers=H(csrf)).status_code == 403
    assert c.get("/admin").status_code == 404                       # the API no longer serves any HTML pages


def test_removed_features_have_no_endpoints(make_client, db, settings):
    adm = make_client()
    csrf = login(adm, f"boss@{DOMAIN}")
    for path in REMOVED:
        assert adm.get(path).status_code == 404, path
    for path in ("/admin/api/corrections", "/admin/api/day-status", "/admin/api/report/regenerate",
                 "/admin/api/report/import-hr-sheets", "/admin/api/cases/1/review", "/admin/api/notifications/1/send"):
        assert adm.post(path, json={}, headers=H(csrf)).status_code in (404, 405), path


def test_admin_dashboard(make_client, db, settings, freeze):
    freeze(2026, 11, 4, 12, 0)
    add_shift(db)
    add_master(db, settings, shift="General", department="Eng")
    add_master(db, settings, "EMP002", "Jane", f"jane@{DOMAIN}")             # no shift -> config issue
    admin = make_client()
    login(admin, f"boss@{DOMAIN}")
    me = admin.get("/admin/api/me").json()
    assert me["email"] == f"boss@{DOMAIN}" and me["role"] == "SUPER_ADMIN" and me["csrf"]
    d = admin.get("/admin/api/dashboard").json()
    assert d["total_active_employees"] == 2 and sorted(d["not_punched_in"]) == ["EMP001", "EMP002"]
    assert d["shift_config_issues"] == [{"employee_id": "EMP002", "employee_name": "Jane", "issue": "NO_SHIFT_ASSIGNED"}]
    assert d["by_department"]["Eng"] == {"total": 1, "present": 0} and "General" in d["by_shift"]
    assert "by_work_mode" not in d and all("mode" not in r for r in d["rows"])
    for gone in ("wfh", "leave_or_holiday"):
        assert gone not in d                                                   # day-status feature is gone
    only = admin.get("/admin/api/dashboard?department=Eng&shift=General").json()
    assert only["total_active_employees"] == 1


def test_admin_sees_all_remarks_with_two_statuses(make_client, shifted, db, settings, freeze):
    c, csrf = shifted
    freeze(2026, 11, 4, 10, 45)                       # General shift starts 09:00 (tolerance 10) -> a late arrival
    rid = punch(c, csrf, "IN").json()["remark_required"]["remark_id"]
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    rows = adm.get("/admin/api/remarks").json()["remarks"]
    assert rows[0]["remark_status"] == "Pending Employee Remark" and rows[0]["employee_id"] == "EMP001"
    c.post(f"/api/remarks/{rid}/submit", json={"reason_category": "Other", "remark": "bus broke down"}, headers=H(csrf))
    assert len(adm.get("/admin/api/remarks?status=Remark%20Submitted").json()["remarks"]) == 1
    assert adm.get("/admin/api/remarks?status=Pending%20Employee%20Remark").json()["remarks"] == []
    assert adm.get("/admin/api/remarks?status=Resolved").status_code == 422
    d = adm.get("/admin/api/dashboard").json()
    assert d["late_arrivals"] == ["EMP001"] and d["remarks_submitted"] == 1 and d["remarks_pending"] == 0


def test_auditor_is_read_only(make_client, db):
    db.add(AdminUser(email=f"aud@{DOMAIN}", role="AUDITOR", status="ACTIVE"))
    db.commit()
    c = make_client()
    csrf = login(c, f"aud@{DOMAIN}")
    assert c.get("/admin/api/dashboard").status_code == 200
    assert c.post("/admin/api/employees/X/status", json={"status": "INACTIVE"}, headers=H(csrf)).status_code == 403
    assert c.post("/admin/api/agent/daily-summary", headers=H(csrf)).status_code == 403


def test_admin_reauth_window_and_disable(make_client, db, settings, monkeypatch):
    c = make_client()
    login(c, f"boss@{DOMAIN}")
    assert c.get("/admin/api/dashboard").status_code == 200
    db.query(AdminUser).update({"status": "DISABLED"})
    db.commit()
    assert c.get("/admin/api/dashboard").status_code == 403
    monkeypatch.setattr(settings, "admin_reauth_minutes", -1)
    db.query(AdminUser).update({"status": "ACTIVE"})
    db.commit()
    assert c.get("/admin/api/dashboard").status_code == 401


def test_employee_activate_deactivate_is_audited_and_blocks_punching(make_client, registered, db):
    emp_client, emp_csrf = registered
    adm = make_client()
    csrf = login(adm, f"boss@{DOMAIN}")
    assert adm.post("/admin/api/employees/emp001/status", json={"status": "INACTIVE"}, headers=H(csrf)).status_code == 200
    assert punch(emp_client, emp_csrf, "IN").status_code == 403
    row = db.scalar(select(AuditLog).where(AuditLog.action == "EMPLOYEE_STATUS"))
    assert row.actor == f"boss@{DOMAIN}" and row.meta == {"from": "ACTIVE", "to": "INACTIVE"}
    assert adm.post("/admin/api/employees/emp001/status", json={"status": "ACTIVE"}, headers=H(csrf)).status_code == 200
    assert punch(emp_client, emp_csrf, "IN").status_code == 200
    assert adm.post("/admin/api/employees/NOPE/status", json={"status": "ACTIVE"}, headers=H(csrf)).status_code == 404
    listing = adm.get("/admin/api/employees").json()["employees"]
    assert listing[0]["employee_id"] == "EMP001" and listing[0]["registered"] is True


def test_attendance_search_filters(make_client, db, settings):
    add_shift(db)
    add_master(db, settings, shift="General", department="Eng")
    add_master(db, settings, "EMP002", "Jane", f"jane@{DOMAIN}", department="HR")
    e = db.scalar(select(Employee).where(Employee.employee_id == "EMP001"))
    db.add(AttendanceEvent(employee_id=e.id, event_type="IN", event_timestamp=datetime.now(UTC) - timedelta(hours=3),
                           source="QR", network_verified=False, recorded_by="x"))
    db.commit()
    c = make_client()
    login(c, f"boss@{DOMAIN}")
    rows = c.get("/admin/api/attendance?employee=john").json()["rows"]
    assert len(rows) == 1 and rows[0]["employee_id"] == "EMP001" and rows[0]["open"] and "mode" not in rows[0]
    assert c.get("/admin/api/attendance?employee=jane").json()["rows"] == []
    assert len(c.get("/admin/api/attendance?department=eng&shift=general").json()["rows"]) == 1
    assert c.get("/admin/api/attendance?department=HR").json()["rows"] == []
    assert c.get("/admin/api/attendance?date_from=bad").status_code == 422


def test_monthly_endpoint_validation_and_content(make_client, db, settings, freeze):
    freeze(2026, 11, 6, 12, 0)
    add_master(db, settings)
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    m = adm.get("/admin/api/monthly?month=2026-11").json()
    assert m["month"] == "2026-11" and m["employees"][0]["absent"] == 4       # Mon 2 .. Thu 5 absent; today (Fri 6) not counted
    for gone in ("leave", "holiday", "approved"):
        assert gone not in m["employees"][0]
    assert adm.get("/admin/api/monthly?month=2026-13").status_code == 422


def test_events_are_append_only(registered, db):
    c, csrf = registered
    punch(c, csrf, "IN")
    ev = db.scalar(select(AttendanceEvent))
    with pytest.raises(DatabaseError):
        db.execute(text("UPDATE attendance_events SET event_type='OUT'"))
    db.rollback()
    with pytest.raises(DatabaseError):
        db.execute(text("DELETE FROM attendance_events"))
    db.rollback()
    ev.event_type = "OUT"
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()
    assert db.scalar(select(AttendanceEvent)).event_type == "IN"


def test_latest_monitor_summary_is_visible_to_admins_only(make_client, registered, db):
    emp, _ = registered
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    assert adm.get("/admin/api/agent/monitor/latest").json() == {"latest": None}
    db.add(AuditLog(actor="agent:system", action="AGENT_MONITOR_RUN", entity_type="agent",
                    meta={"status": "complete", "answer": "1 missing punch out (EMP001).", "counts": {"missing_punches": 1},
                          "tools": ["detect_missing_punches"], "human_review_required": True}))
    db.commit()
    latest = adm.get("/admin/api/agent/monitor/latest").json()["latest"]
    assert latest["status"] == "complete" and latest["answer"].startswith("1 missing") and latest["human_review_required"] is True
    assert emp.get("/admin/api/agent/monitor/latest").status_code == 403
