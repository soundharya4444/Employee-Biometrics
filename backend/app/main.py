"""Backend API only. The web UI is a separate project in ../frontend (built with `npm run build`)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.auth.routes import router as auth_router
from app.config import get_settings
from app.database.connection import SessionLocal, init_db
from app.routes.admin import router as admin_router
from app.routes.assistant import router as assistant_router
from app.routes.attendance import router as attendance_router
from app.routes.employee import router as employee_router
from app.services.employee_service import seed_bootstrap_admins
from app.services.excel_generator import worker
from app.services.monitor import monitor_worker

log = logging.getLogger("attendance")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_for_startup()
    if not settings.is_production:
        settings.reports_dir.parent.mkdir(parents=True, exist_ok=True)
        init_db()  # dev convenience; production uses `alembic upgrade head`
    with SessionLocal() as db:
        seed_bootstrap_admins(db, settings)
    worker.start()
    worker.request_update()
    if settings.monitor_enabled:
        monitor_worker.start(settings)
    yield
    worker.stop()
    monitor_worker.stop()


app = FastAPI(title="GWC Data.AI Employee Attendance API", lifespan=lifespan,
              docs_url=None if settings.is_production else "/docs",
              redoc_url=None, openapi_url=None if settings.is_production else "/openapi.json")

# Session cookie: HTTP-only, signed. SameSite=Lax works when frontend and API share a registrable domain
# (app.example.com + api.example.com, cookie_domain=".example.com"); SameSite=None (+Secure) for different sites.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="attendance_session",
                   max_age=settings.session_max_age_seconds, same_site=settings.cookie_samesite.lower(),
                   https_only=settings.cookie_secure, domain=settings.cookie_domain or None)
# CORS: only the configured frontend origin(s), with credentials (cookie) and our CSRF header.
app.add_middleware(CORSMiddleware, allow_origins=settings.frontend_origins, allow_credentials=True,
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token"],
                   expose_headers=["Content-Disposition"], max_age=600)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    if settings.cookie_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


app.include_router(auth_router)
app.include_router(employee_router)
app.include_router(attendance_router)
app.include_router(admin_router)
app.include_router(assistant_router)
# Nothing static is served: the reports folder and the UI are NOT exposed by this API.


@app.get("/", include_in_schema=False)
def root():
    return {"service": "attendance-api", "ok": True}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
