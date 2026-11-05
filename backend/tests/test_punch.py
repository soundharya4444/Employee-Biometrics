import ipaddress
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database.models import AttendanceEvent, AuditLog, Employee
from app.services.client_ip import resolve_client_ip
from conftest import DOMAIN, add_master, login, punch, register

H = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731


def test_punch_in_out_flow(registered, db):
    c, csrf = registered
    t = c.get("/api/attendance/today").json()
    assert t["punch_in"] == "Not yet punched" and not t["is_punched_in"]
    r = punch(c, csrf, "IN")
    assert r.status_code == 200
    a = r.json()["attendance"]
    assert a["is_punched_in"] and a["working_hours"] == "In progress" and a["punch_out"] == "Not yet punched"
    assert punch(c, csrf, "IN").status_code == 409          # conflicting open session
    r = punch(c, csrf, "OUT")
    assert r.status_code == 200 and not r.json()["attendance"]["is_punched_in"]
    assert punch(c, csrf, "OUT").status_code == 409         # no open IN
    assert db.query(AttendanceEvent).count() == 2


def test_multiple_sessions_never_overwrite(registered, db):
    c, csrf = registered
    for t in ("IN", "OUT", "IN", "OUT"):
        assert punch(c, csrf, t).status_code == 200
    assert db.query(AttendanceEvent).count() == 4
    assert len(c.get("/api/attendance/today").json()["sessions"]) == 2


def test_idempotency_key_prevents_duplicate(registered, db):
    c, csrf = registered
    a = punch(c, csrf, "IN", "key-1")
    b = punch(c, csrf, "IN", "key-1")
    assert a.status_code == b.status_code == 200
    assert db.query(AttendanceEvent).count() == 1


def test_too_fast_punch_rejected(registered, settings, monkeypatch):
    c, csrf = registered
    monkeypatch.setattr(settings, "min_seconds_between_punches", 30)
    assert punch(c, csrf, "IN").status_code == 200
    assert punch(c, csrf, "OUT").status_code == 429


def test_timestamp_is_server_time(registered, db):
    c, csrf = registered
    r = c.post("/api/attendance/punch", json={"type": "IN", "timestamp": "2000-01-01T00:00:00Z"},
               headers=H(csrf))
    assert r.status_code == 200
    ev = db.scalar(select(AttendanceEvent))
    assert abs((datetime.now(UTC) - ev.event_timestamp).total_seconds()) < 10


def test_inactive_employee_blocked(registered, db):
    c, csrf = registered
    db.query(Employee).update({"account_status": "INACTIVE"})
    db.commit()
    assert punch(c, csrf, "IN").status_code == 403


# ---- no network rule: any Wi-Fi / mobile data works, punching is one tap ----
def test_punch_works_from_any_network_address(make_client, db, settings):
    add_master(db, settings)
    for ip in ("203.0.113.5", "198.51.100.77", "10.1.2.3", "2001:db8::1"):
        c = make_client(ip)
        csrf = login(c, f"john.jack@{DOMAIN}")
        c.post("/api/register", json={"employee_id": "EMP001", "employee_name": "x"}, headers=H(csrf))
        r = punch(c, csrf, "IN")
        assert r.status_code == 200, (ip, r.text)
        assert punch(c, csrf, "OUT").status_code == 200
    ips = {e.client_ip for e in db.scalars(select(AttendanceEvent))}
    assert {"203.0.113.5", "198.51.100.77", "10.1.2.3", "2001:db8::1"} == ips        # recorded for audit only


def test_punch_is_audited_with_ip(registered, db):
    c, csrf = registered
    punch(c, csrf, "IN")
    a = db.scalar(select(AuditLog).where(AuditLog.action == "PUNCH_IN"))
    assert a.meta["client_ip"] and a.meta["attendance_id"] and "work_mode" not in a.meta


def test_resolve_client_ip_rules():
    trusted = [ipaddress.ip_network("10.0.0.0/8")]
    assert resolve_client_ip("198.51.100.1", "1.2.3.4", trusted) == ("198.51.100.1", False)   # spoof ignored
    assert resolve_client_ip("10.0.0.5", "1.2.3.4, 9.9.9.9", trusted) == ("9.9.9.9", True)    # right-most untrusted
    assert resolve_client_ip("10.0.0.5", "203.0.113.5, 10.0.0.9", trusted) == ("203.0.113.5", True)
    assert resolve_client_ip("10.0.0.5", "garbage", trusted) == (None, True)
    assert resolve_client_ip("testclient", None, trusted) == (None, False)


def test_stale_open_session_is_left_open_and_new_in_allowed(registered, db):
    c, csrf = registered
    emp = db.scalar(select(Employee))
    old = AttendanceEvent(employee_id=emp.id, event_type="IN", event_timestamp=datetime.now(UTC) - timedelta(hours=30),
                          source="QR", network_verified=False, recorded_by="x")
    db.add(old)
    db.commit()
    assert punch(c, csrf, "IN").status_code == 200
    assert db.query(AttendanceEvent).filter_by(event_type="OUT").count() == 0          # no OUT is ever invented
    a = db.scalar(select(AuditLog).where(AuditLog.action == "STALE_SESSION_LEFT_OPEN"))
    assert a is not None and a.entity_id == str(old.attendance_id)


def test_cannot_read_other_employees_data(make_client, db, settings):
    add_master(db, settings)
    add_master(db, settings, "EMP002", "Jane", f"jane@{DOMAIN}")
    a, b = make_client(), make_client()
    ca = register(a, f"john.jack@{DOMAIN}", "EMP001")
    cb = register(b, f"jane@{DOMAIN}", "EMP002")
    punch(a, ca, "IN")
    assert b.get("/api/attendance/today").json()["punch_in"] == "Not yet punched"
    assert b.get("/api/attendance/history").json()["history"] == []
    assert b.get("/api/me").json()["employee"]["employee_id"] == "EMP002"
    assert cb


def test_punch_in_is_a_single_tap_with_no_extra_fields(registered, db):
    c, csrf = registered
    r = c.post("/api/attendance/punch", json={"type": "IN"}, headers=H(csrf))
    assert r.status_code == 200 and "work_mode" not in r.json() and "work_mode" not in r.json()["attendance"]
