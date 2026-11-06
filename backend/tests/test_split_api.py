import io
from datetime import date, datetime

from openpyxl import load_workbook
from sqlalchemy import select

from app.database.models import AttendanceEvent, Employee
from conftest import DOMAIN, add_master, add_shift, ist, login

FRONT = "http://localhost:5173"


def test_cors_allows_only_the_configured_frontend(client):
    ok = client.options("/api/me", headers={"Origin": FRONT, "Access-Control-Request-Method": "GET"})
    assert ok.headers["access-control-allow-origin"] == FRONT and ok.headers["access-control-allow-credentials"] == "true"
    pre = client.options("/api/attendance/punch", headers={"Origin": FRONT, "Access-Control-Request-Method": "POST",
                                                           "Access-Control-Request-Headers": "content-type,x-csrf-token"})
    assert pre.status_code == 200 and "x-csrf-token" in pre.headers["access-control-allow-headers"].lower()
    bad = client.options("/api/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
    r = client.get("/api/me", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers          # browser would refuse to expose the response


def test_state_changing_request_from_foreign_origin_is_blocked(registered):
    c, csrf = registered
    ok = c.post("/api/attendance/punch", json={"type": "IN"}, headers={"X-CSRF-Token": csrf, "Origin": FRONT})
    assert ok.status_code == 200
    bad = c.post("/api/attendance/punch", json={"type": "OUT"}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert bad.status_code == 403


def test_api_serves_no_ui_or_files(client):
    assert client.get("/").json() == {"service": "attendance-api", "ok": True}
    for p in ("/static/js/employee.js", "/static/css/app.css", "/admin", "/storage/reports/attendance.xlsx"):
        assert client.get(p).status_code == 404


def test_dev_login_is_json_and_domain_restricted(client, settings, monkeypatch):
    assert client.post("/api/auth/dev-login", json={"email": f"a@{DOMAIN}"}).json() == {"ok": True}
    assert client.get("/api/me").json()["state"] == "unregistered"
    monkeypatch.setattr(settings, "dev_auth_enabled", False)
    assert client.post("/api/auth/dev-login", json={"email": f"a@{DOMAIN}"}).status_code == 404
    assert client.get("/api/me").json().get("dev_auth") is None or client.get("/api/me").json()["state"] != "x"


def test_unauthenticated_me_tells_frontend_whether_dev_login_exists(client):
    assert client.get("/api/me").json() == {"state": "unauthenticated", "domain": DOMAIN, "dev_auth": True, "poc_auth": False}


def test_login_redirect_goes_back_to_frontend_in_dev(client, settings):
    r = client.get("/auth/login?next=/admin/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == settings.frontend_url.rstrip("/") + "/admin/"
    r = client.get("/auth/login?next=https://evil.example", follow_redirects=False)   # open-redirect guard
    assert r.headers["location"] == settings.frontend_url.rstrip("/") + "/"


def seed(db, settings):
    add_shift(db)
    add_master(db, settings, "E1", "Asha", "asha@gwcdata.ai", shift="General")
    e = db.scalar(select(Employee))
    for d in (3, 4):
        db.add(AttendanceEvent(employee_id=e.id, event_type="IN", event_timestamp=ist(2026, 11, d, 9, 30), source="QR",
                               network_verified=True, recorded_by="t"))
        db.add(AttendanceEvent(employee_id=e.id, event_type="OUT", event_timestamp=ist(2026, 11, d, 18, 30), source="QR",
                               network_verified=True, recorded_by="t"))
    db.commit()


def sheets(resp):
    return load_workbook(io.BytesIO(resp.content))


def test_full_and_attendance_only_downloads(make_client, db, settings, freeze):
    freeze(2026, 11, 6, 12, 0)
    seed(db, settings)
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    full = adm.get("/admin/api/report/download?scope=full")
    assert full.status_code == 200 and sheets(full).sheetnames == [
        "Daily Attendance", "Raw Punch Logs", "Monthly Summary", "Shift Master", "Employee Master"]
    assert "attendance_full_" in full.headers["content-disposition"]
    only = adm.get("/admin/api/report/download?scope=attendance")
    assert only.status_code == 200 and sheets(only).sheetnames == ["Daily Attendance"]     # attendance alone
    assert "attendance_only_" in only.headers["content-disposition"] and only.headers["cache-control"] == "no-store"
    ws = sheets(only)["Daily Attendance"]
    assert ws["B3"].value == "Asha"
    default = adm.get("/admin/api/report/download")                                        # default stays the full workbook
    assert len(sheets(default).sheetnames) == 5


def test_attendance_only_respects_date_range(make_client, db, settings, freeze):
    freeze(2026, 11, 6, 12, 0)
    seed(db, settings)
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    one = sheets(adm.get("/admin/api/report/download?scope=attendance&date_from=2026-11-04&date_to=2026-11-04"))["Daily Attendance"]
    heads = [one.cell(1, c).value for c in range(4, one.max_column + 1, 2) if one.cell(1, c).value]
    assert [h.date() if isinstance(h, datetime) else h for h in heads] == [date(2026, 11, 4)]
    blank = sheets(adm.get("/admin/api/report/download?scope=attendance&date_from=2026-12-01&date_to=2026-12-02"))["Daily Attendance"]
    heads = [blank.cell(1, c).value for c in range(4, blank.max_column + 1, 2) if blank.cell(1, c).value]
    assert [h.date() if isinstance(h, datetime) else h for h in heads] == [date(2026, 12, 1), date(2026, 12, 2)]
    assert adm.get("/admin/api/report/download?scope=attendance&date_from=2026-11-05&date_to=2026-11-01").status_code == 422
    assert adm.get("/admin/api/report/download?scope=bogus").status_code == 422
    assert adm.get("/admin/api/report/download?scope=attendance&date_from=nope").status_code == 422


def test_downloads_are_admin_only_and_audited(make_client, registered, db):
    c, _ = registered
    assert c.get("/admin/api/report/download?scope=attendance").status_code == 403
    assert make_client().get("/admin/api/report/download?scope=attendance").status_code == 401
    adm = make_client()
    login(adm, f"boss@{DOMAIN}")
    adm.get("/admin/api/report/download?scope=attendance")
    from app.database.models import AuditLog
    row = db.scalars(select(AuditLog).where(AuditLog.action == "REPORT_DOWNLOAD").order_by(AuditLog.audit_id.desc())).first()
    assert row.meta["scope"] == "attendance" and row.actor == f"boss@{DOMAIN}"


def test_production_config_requires_https_frontend_and_secure_samesite_none(settings, monkeypatch):
    import pytest
    for k, v in dict(app_env="production", secret_key="x" * 40, cookie_secure=True, dev_auth_enabled=False,
                     google_client_id="a", google_client_secret="b",
                     database_url="postgresql://u:p@h/db").items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(settings, "frontend_url", "http://app.example.com")
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        settings.validate_for_startup()
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    monkeypatch.setattr(settings, "cookie_samesite", "none")
    monkeypatch.setattr(settings, "cookie_secure", False)
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        settings.validate_for_startup()
    monkeypatch.setattr(settings, "cookie_secure", True)
    settings.validate_for_startup()
