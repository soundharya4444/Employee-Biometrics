"""Company sign-in: Google Workspace OpenID Connect restricted to the company domain.
A dev-only JSON login exists for local development and is refused at startup in production.
After sign-in the browser is sent back to the separate frontend (settings.frontend_url)."""
from __future__ import annotations

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import csrf_protect, login_session, rate_limit, settings_dep
from app.config import Settings
from app.database.connection import get_db
from app.services.audit import audit
from app.services.employee_service import email_in_domain

router = APIRouter(tags=["auth"])
oauth = OAuth()
_registered = False


def _google(settings: Settings):
    global _registered
    if not _registered:
        oauth.register(name="google", client_id=settings.google_client_id,
                       client_secret=settings.google_client_secret,
                       server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})
        _registered = True
    return oauth.google


def _safe_next(n: str | None) -> str:
    return n if n in ("/", "/admin/") else "/"


def _to_frontend(settings: Settings, path: str) -> str:
    return settings.frontend_url.rstrip("/") + path


@router.get("/auth/login", dependencies=[Depends(rate_limit("auth", "auth_rate_limit"))])
async def login(request: Request, next: str = "/", settings: Settings = Depends(settings_dep)):
    nxt = _safe_next(next)
    if not (settings.google_client_id and settings.google_client_secret):
        if settings.dev_auth_enabled:  # frontend shows its dev sign-in form (see /api/me -> dev_auth)
            return RedirectResponse(_to_frontend(settings, nxt))
        raise HTTPException(503, "Company sign-in is not configured (GOOGLE_CLIENT_ID/SECRET).")
    request.session["next"] = nxt
    redirect_uri = settings.base_url.rstrip("/") + "/auth/callback"
    return await _google(settings).authorize_redirect(request, redirect_uri, hd=settings.allowed_email_domain,
                                                      prompt="select_account")


@router.get("/auth/callback", dependencies=[Depends(rate_limit("auth", "auth_rate_limit"))])
async def callback(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    try:
        token = await _google(settings).authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(400, f"Sign-in failed: {exc.error}")
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    ok = (info.get("email_verified") is True and email_in_domain(email, settings)
          and (info.get("hd") or "").lower() == settings.allowed_email_domain)
    if not ok:
        audit(db, email or "unknown", "LOGIN_REJECTED", "auth", None, {"reason": "domain/verification"})
        db.commit()
        request.session.clear()
        raise HTTPException(403, f"Only verified @{settings.allowed_email_domain} accounts may sign in.")
    nxt = _safe_next(request.session.get("next"))
    login_session(request, email=email, sub=info.get("sub"), name=info.get("name"))
    audit(db, email, "LOGIN", "auth", None, {})
    db.commit()
    return RedirectResponse(_to_frontend(settings, nxt))


class DevLoginIn(BaseModel):
    email: str = Field(max_length=320)


@router.post("/api/auth/dev-login", dependencies=[Depends(rate_limit("auth", "auth_rate_limit"))])
def dev_login(body: DevLoginIn, request: Request, db: Session = Depends(get_db),
              settings: Settings = Depends(settings_dep)):
    """DEV ONLY - not company authentication. 404 unless DEV_AUTH_ENABLED (never allowed in production)."""
    if not settings.dev_auth_enabled or settings.is_production:
        raise HTTPException(404)
    email = body.email.strip().lower()
    if not email_in_domain(email, settings):
        raise HTTPException(403, f"Only @{settings.allowed_email_domain} accounts are allowed")
    login_session(request, email=email, sub=f"dev:{email}", name=email.split("@")[0])
    audit(db, email, "LOGIN_DEV", "auth", None, {})
    db.commit()
    return {"ok": True}


@router.post("/auth/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
