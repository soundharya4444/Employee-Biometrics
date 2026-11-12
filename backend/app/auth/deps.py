"""Session identity, CSRF, rate limiting and authorisation dependencies."""
from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.connection import get_db
from app.database.models import AdminUser, Employee
from app.services.employee_service import employee_by_identity, get_admin
from app.services.client_ip import resolve_client_ip


def settings_dep() -> Settings:
    return get_settings()


# ---------- identity ----------
def login_session(request: Request, *, email: str, sub: str | None, name: str | None) -> None:
    request.session.clear()
    request.session.update({"email": email.lower(), "sub": sub, "name": name,
                            "auth_time": int(time.time()), "csrf": secrets.token_urlsafe(32)})


def current_identity(request: Request, settings: Settings = Depends(settings_dep)) -> dict | None:
    s = request.session
    if not s.get("email") or not s.get("auth_time"):
        return None
    if time.time() - s["auth_time"] > settings.session_max_age_seconds:
        request.session.clear()
        return None
    return dict(s)


def require_identity(ident: dict | None = Depends(current_identity)) -> dict:
    if not ident:
        raise HTTPException(401, "Authentication required")
    return ident


def require_employee(ident: dict = Depends(require_identity), db: Session = Depends(get_db)) -> Employee:
    emp = employee_by_identity(db, ident.get("sub"), ident["email"])
    if not emp:
        raise HTTPException(403, "Not registered")
    return emp


def require_admin(write: bool = False):
    def dep(request: Request, ident: dict = Depends(require_identity), db: Session = Depends(get_db),
            settings: Settings = Depends(settings_dep)) -> AdminUser:
        admin = get_admin(db, ident["email"])
        if not admin:
            raise HTTPException(403, "Admin access denied")
        if time.time() - ident["auth_time"] > settings.admin_reauth_minutes * 60:
            raise HTTPException(401, "Admin re-authentication required")
        if write and admin.role == "AUDITOR":
            raise HTTPException(403, "Read-only role")
        return admin
    return dep


# ---------- CSRF (session-bound token, sent as X-CSRF-Token header by our JS) ----------
def csrf_protect(request: Request, settings: Settings = Depends(settings_dep)) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    if not request.session.get("email"):
        raise HTTPException(401, "Authentication required")
    sent = request.headers.get("x-csrf-token", "")
    if not expected or not hmac.compare_digest(expected, sent):
        raise HTTPException(403, "CSRF token missing or invalid")
    origin = request.headers.get("origin")
    allowed = {o.rstrip("/") for o in settings.frontend_origins} | {settings.base_url.rstrip("/"),
                                                                     str(request.base_url).rstrip("/")}
    if origin and origin.rstrip("/") not in allowed and not settings.origin_allowed(origin):
        raise HTTPException(403, "Cross-origin request blocked")


# ---------- rate limiting (in-memory sliding window; use a shared store if you run many workers) ----------
_hits: dict[str, deque] = defaultdict(deque)


def reset_rate_limits() -> None:
    _hits.clear()


def rate_limit(name: str, limit_attr: str, per: int = 60):
    def dep(request: Request, settings: Settings = Depends(settings_dep)) -> None:
        peer = request.client.host if request.client else "unknown"
        ip, _ = resolve_client_ip(peer, request.headers.get("x-forwarded-for"), settings.trusted_proxy_list)
        key = f"{name}:{ip or peer}:{request.session.get('email', '')}"
        limit = getattr(settings, limit_attr)
        now = time.monotonic()
        q = _hits[key]
        while q and now - q[0] > per:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many requests. Please slow down.")
        q.append(now)
    return dep
