import os
import tempfile
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

_tmp = tempfile.mkdtemp(prefix="attendance-tests-")
os.environ.update({
    "APP_ENV": "development", "DATABASE_URL": "sqlite://", "SECRET_KEY": "test-secret-key-test-secret-key-123",
    "DEV_AUTH_ENABLED": "true", "REPORTS_DIR": str(Path(_tmp) / "reports"),
    "BOOTSTRAP_ADMIN_EMAILS": "boss@gwcdata.ai,boss2@gwcdata.ai", "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "",
    "MIN_SECONDS_BETWEEN_PUNCHES": "0", "PUNCH_RATE_LIMIT": "1000", "AUTH_RATE_LIMIT": "1000",
    "ASSISTANT_RATE_LIMIT": "1000", "DOMO_INSTANCE_URL": "", "DOMO_DEVELOPER_TOKEN": "", "DOMO_LLM_ENDPOINT_PATH": "",
    "MONITOR_ENABLED": "false", "ATTENDANCE_POLICY_FILE": "",
    # pinned so the tests never depend on the developer's real .env
    "BASE_URL": "http://localhost:8000", "FRONTEND_URL": "http://localhost:5173",
    "COOKIE_SAMESITE": "lax", "COOKIE_DOMAIN": "", "TRUSTED_PROXIES": "", "REQUIRE_MASTER_LIST": "true",
    "COOKIE_SECURE": "false", "TIMEZONE": "Asia/Kolkata", "DEFAULT_SHIFT_START": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth.deps import reset_rate_limits  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import models  # noqa: E402
from app.database.connection import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import attendance_service as att  # noqa: E402
from app.services import excel_generator  # noqa: E402
from app.services.employee_service import upsert_master_employee  # noqa: E402

DOMAIN = "gwcdata.ai"
IST = ZoneInfo("Asia/Kolkata")


def ist(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Background report worker is disabled in tests (in-memory SQLite is single-connection); tests call it explicitly.
    monkeypatch.setattr(excel_generator.ReportWorker, "start", lambda self: None)
    monkeypatch.setattr(excel_generator.ReportWorker, "request_update", lambda self: setattr(self, "requested", True))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        models.install_append_only_triggers(c)
    reset_rate_limits()
    excel_generator.status.update({"generated_at": None, "error": None, "hr_sync": None})
    rp = get_settings().reports_dir
    if rp.exists():
        for f in rp.iterdir():
            f.unlink()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def freeze(monkeypatch):
    """Freeze the backend clock at an Asia/Kolkata wall-clock time."""
    def _set(y, m, d, hh=0, mm=0):
        ts = ist(y, m, d, hh, mm)
        monkeypatch.setattr(att, "now_utc", lambda: ts)
        return ts
    return _set


@pytest.fixture
def make_client():
    clients = []

    def factory(ip: str = "198.51.100.7") -> TestClient:
        c = TestClient(app, client=(ip, 50000))
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


def login(c: TestClient, email: str) -> str:
    r = c.post("/api/auth/dev-login", json={"email": email})
    assert r.status_code == 200, r.text
    return c.get("/api/me").json()["csrf"]


def add_shift(db, name="General", start=time(9, 0), end=time(18, 0), late=10, early=10, active=True):
    db.add(models.ShiftMaster(shift_name=name, start_time=start, end_time=end, late_tolerance_minutes=late,
                              early_tolerance_minutes=early, active=active))
    db.commit()


def add_master(db, settings, eid="EMP001", name="John", email=f"john.jack@{DOMAIN}", shift=None, **kw):
    upsert_master_employee(db, settings, employee_id=eid, name=name, email=email, assigned_shift=shift, **kw)
    db.commit()


def register(client, email, eid):
    csrf = login(client, email)
    r = client.post("/api/register", json={"employee_id": eid, "employee_name": "x"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    return csrf


@pytest.fixture
def registered(client, db, settings):
    """A registered, active employee (no shift assigned) with a logged-in client."""
    add_master(db, settings)
    return client, register(client, f"john.jack@{DOMAIN}", "emp001")


@pytest.fixture
def shifted(client, db, settings):
    """Registered employee on shift 'General' 09:00-18:00, tolerance 10/10."""
    add_shift(db)
    add_master(db, settings, shift="General")
    return client, register(client, f"john.jack@{DOMAIN}", "emp001")


def punch(c, csrf, typ, key=None):
    body = {"type": typ}
    if key:
        body["request_key"] = key
    return c.post("/api/attendance/punch", json=body, headers={"X-CSRF-Token": csrf})
