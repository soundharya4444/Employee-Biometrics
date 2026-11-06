from conftest import DOMAIN  # noqa: F401

PHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"}
PC = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def test_phone_and_tablet_are_blocked(client):
    r = client.get("/api/attendance/time", headers=PHONE)
    assert r.status_code == 403 and r.json()["code"] == "RESTRICTED" and r.json()["reason"] == "DEVICE"
    assert client.get("/api/attendance/time", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-T870)"}).status_code == 403
    assert client.get("/api/attendance/time", headers={**PC, "Sec-CH-UA-Mobile": "?1"}).status_code == 403
    assert client.get("/api/attendance/time", headers=PC).status_code == 200
    assert client.get("/api/me", headers=PHONE).status_code == 200  # sign-in is open; only attendance calls are restricted


def test_only_office_network_allowed(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "office_ip_ranges", "203.0.113.0/28")  # test client IP is 198.51.100.7
    r = client.get("/api/attendance/time", headers=PC)
    assert r.status_code == 403 and r.json()["reason"] == "NETWORK" and r.json()["client_ip"] == "198.51.100.7"
    monkeypatch.setattr(settings, "office_ip_ranges", "198.51.100.0/24")
    assert client.get("/api/attendance/time", headers=PC).status_code == 200


def test_policy_can_be_switched_off(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "desktop_only", False)
    assert client.get("/api/attendance/time", headers=PHONE).status_code == 200


def test_hr_admin_portal_is_never_restricted(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "office_ip_ranges", "203.0.113.0/28")  # foreign network
    assert client.get("/api/attendance/time", headers=PHONE).status_code == 403
    r = client.get("/admin/api/employees", headers=PHONE)
    assert r.status_code != 403 or r.json().get("code") != "RESTRICTED"  # 401/403 from auth, never RESTRICTED
    assert client.get("/api/me", headers=PHONE).status_code == 200
