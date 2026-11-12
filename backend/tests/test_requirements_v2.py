from datetime import time

from conftest import DOMAIN, add_master, add_shift, login, punch, register
from app.database.models import AttendanceEvent

PC = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def test_punch_in_unavailable_before_0845(registered, settings, freeze, monkeypatch):
    c, csrf = registered
    monkeypatch.setattr(settings, "punch_in_opens_at", "08:45")
    freeze(2026, 11, 5, 8, 44)
    t = c.get("/api/attendance/today").json()
    assert t["can_punch_in"] is False and t["punch_in_opens_at"] == "08:45"
    r = punch(c, csrf, "IN")
    assert r.status_code == 403 and r.json()["code"] == "too_early"
    freeze(2026, 11, 5, 8, 45)
    assert c.get("/api/attendance/today").json()["can_punch_in"] is True
    assert punch(c, csrf, "IN").status_code == 200


def test_example_day_first_in_and_latest_out(registered, db, freeze):
    c, csrf = registered
    for hh, mm, kind in [(8, 50, "IN"), (10, 30, "OUT"), (11, 15, "IN"), (13, 0, "OUT"), (14, 0, "IN"), (18, 10, "OUT")]:
        freeze(2026, 11, 5, hh, mm)
        assert punch(c, csrf, kind).status_code == 200
    t = c.get("/api/attendance/today").json()
    assert t["punch_in"] == "08:50 AM" and t["punch_out"] == "06:10 PM"      # first IN + latest OUT
    assert len(t["sessions"]) == 3                                          # intermediate punches kept
    assert db.query(AttendanceEvent).count() == 6
    assert t["working_hours"] == "7h 35m"                                    # 1h40 + 1h45 + 4h10 worked


def test_either_approved_ip_is_allowed_others_blocked(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "office_ip_ranges", "192.168.31.169,198.51.100.7")  # test client IP is 198.51.100.7
    assert client.get("/api/attendance/time", headers=PC).status_code == 200          # matches the 2nd entry
    monkeypatch.setattr(settings, "office_ip_ranges", "198.51.100.7,192.168.0.210")
    assert client.get("/api/attendance/time", headers=PC).status_code == 200          # matches the 1st entry
    monkeypatch.setattr(settings, "office_ip_ranges", "192.168.31.169,192.168.0.210")
    r = client.get("/api/attendance/time", headers=PC)
    assert r.status_code == 403 and r.json()["reason"] == "NETWORK"


def test_30_employees_double_submit_makes_one_record_each(make_client, db, settings, freeze):
    """Logic check (sequential). Real concurrency is exercised by scripts/load_test_punch.py against a live server:
    the in-memory test database shares ONE connection, so it cannot be hit from many threads."""
    add_shift(db, "General", time(9, 0), time(18, 0))
    freeze(2026, 11, 5, 9, 0)
    for i in range(30):
        add_master(db, settings, eid=f"E{i:03d}", name=f"Emp {i}", email=f"emp{i}@{DOMAIN}", shift="General")
        c = make_client(ip=f"198.51.100.{10 + i}")
        csrf = register(c, f"emp{i}@{DOMAIN}", f"E{i:03d}")
        assert punch(c, csrf, "IN", f"key-{i}").status_code == 200
        assert punch(c, csrf, "IN", f"key-{i}").status_code == 200   # replay of the same click
    assert db.query(AttendanceEvent).count() == 30
