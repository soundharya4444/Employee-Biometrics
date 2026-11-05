from conftest import DOMAIN, add_master, login


def test_unauthenticated_state(client):
    assert client.get("/api/me").json()["state"] == "unauthenticated"
    assert client.get("/api/attendance/today").status_code == 401


def test_non_company_email_rejected_by_backend(client):
    r = client.post("/api/auth/dev-login", json={"email": "someone@gmail.com"})
    assert r.status_code == 403
    r = client.post("/api/auth/dev-login", json={"email": f"x@evil.com.{DOMAIN}"})
    assert r.status_code == 403
    r = client.post("/api/auth/dev-login", json={"email": f"x@{DOMAIN}.evil.com"})
    assert r.status_code == 403


def test_register_requires_master_list(client):
    csrf = login(client, f"nobody@{DOMAIN}")
    assert client.get("/api/me").json()["state"] == "unregistered"
    r = client.post("/api/register", json={"employee_id": "EMP999", "employee_name": "Nobody"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403


def test_register_and_return(client, db, settings):
    add_master(db, settings)
    csrf = login(client, f"john.jack@{DOMAIN}")
    r = client.post("/api/register", json={"employee_id": "EMP001", "employee_name": "ignored"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    me = client.get("/api/me").json()
    assert me["state"] == "registered"
    assert me["employee"] == {"employee_id": "EMP001", "employee_name": "John", "mail_id": f"john.jack@{DOMAIN}",
                              "department": None, "account_status": "ACTIVE"}
    # duplicate registration
    r = client.post("/api/register", json={"employee_id": "EMP001", "employee_name": "x"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409


def test_cannot_claim_someone_elses_employee_id(make_client, db, settings):
    add_master(db, settings)
    add_master(db, settings, "EMP002", "Jane", f"jane@{DOMAIN}")
    c = make_client()
    csrf = login(c, f"jane@{DOMAIN}")
    r = c.post("/api/register", json={"employee_id": "EMP001", "employee_name": "x"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403
    assert c.get("/api/me").json()["state"] == "unregistered"


def test_csrf_required(client, db, settings):
    add_master(db, settings)
    login(client, f"john.jack@{DOMAIN}")
    r = client.post("/api/register", json={"employee_id": "EMP001", "employee_name": "x"})
    assert r.status_code == 403
    r = client.post("/api/register", json={"employee_id": "EMP001", "employee_name": "x"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


def test_without_master_list_registration_is_pending(client, db, settings, monkeypatch):
    monkeypatch.setattr(settings, "require_master_list", False)
    csrf = login(client, f"new@{DOMAIN}")
    assert client.post("/api/register", json={"employee_id": "N1", "employee_name": "New"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/me").json()["employee"]["account_status"] == "PENDING_APPROVAL"
    r = client.post("/api/attendance/punch", json={"type": "IN"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403  # not active until HR approves


def test_session_expiry(registered, monkeypatch, settings):
    c, _ = registered
    monkeypatch.setattr(settings, "session_max_age_seconds", -1)
    assert c.get("/api/me").json()["state"] == "unauthenticated"


def test_dev_login_disabled_in_production_config(settings, monkeypatch):
    import pytest
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "secret_key", "x" * 40)
    monkeypatch.setattr(settings, "cookie_secure", True)
    monkeypatch.setattr(settings, "google_client_id", "a")
    monkeypatch.setattr(settings, "google_client_secret", "b")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    with pytest.raises(RuntimeError, match="DEV_AUTH_ENABLED"):
        settings.validate_for_startup()


def test_new_device_authenticates_and_gets_existing_profile_not_a_new_registration(make_client, db, settings):
    from sqlalchemy import select
    from app.database.models import Employee
    add_master(db, settings)
    phone = make_client()
    csrf = login(phone, f"john.jack@{DOMAIN}")
    assert phone.post("/api/register", json={"employee_id": "EMP001", "employee_name": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    laptop = make_client()                                             # different browser/device, no cookies
    assert laptop.get("/api/me").json()["state"] == "unauthenticated"  # must authenticate again
    login(laptop, f"john.jack@{DOMAIN}")
    me = laptop.get("/api/me").json()
    assert me["state"] == "registered" and me["employee"]["employee_id"] == "EMP001"   # existing profile, no registration screen
    assert db.query(Employee).count() == 1


def test_returning_employee_state_has_no_registration_prompt(registered):
    c, _ = registered
    me = c.get("/api/me").json()
    assert me["state"] == "registered" and me["employee"]["mail_id"] == "john.jack@gwcdata.ai"
    assert c.get("/api/attendance/today").status_code == 200           # straight to attendance
