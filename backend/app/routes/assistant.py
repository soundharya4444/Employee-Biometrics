"""ONE assistant endpoint for employees and HR. The role and identity come from the authenticated session;
nothing in the request body can change them."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.domo_agent import AssistantAgent
from app.agents.domo_client import DomoClient
from app.agents.tools import ToolContext
from app.auth.deps import csrf_protect, rate_limit, require_identity, settings_dep
from app.config import Settings
from app.database.connection import get_db
from app.services.employee_service import employee_by_identity, get_admin

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(max_length=2000)


class ChatIn(BaseModel):
    message: str = Field(min_length=2, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=12)


def build_context(db: Session, settings: Settings, ident: dict) -> ToolContext | None:
    admin = get_admin(db, ident["email"])
    if admin and time.time() - ident["auth_time"] <= settings.admin_reauth_minutes * 60:
        return ToolContext(db, settings, admin.email, admin.role)
    emp = employee_by_identity(db, ident.get("sub"), ident["email"])
    if emp and emp.account_status == "ACTIVE":
        return ToolContext(db, settings, emp.verified_email, "EMPLOYEE", emp)
    return None


@router.get("/status")
def status(ident: dict = Depends(require_identity), db: Session = Depends(get_db),
           settings: Settings = Depends(settings_dep)):
    ctx = build_context(db, settings, ident)
    c = DomoClient(settings)
    return {"available_to_user": ctx is not None, "role": ctx.role if ctx else None,
            "llm_configured": c.is_configured}  # no config details for employees


@router.post("/chat", dependencies=[Depends(csrf_protect), Depends(rate_limit("assistant", "assistant_rate_limit"))])
def chat(body: ChatIn, ident: dict = Depends(require_identity), db: Session = Depends(get_db),
         settings: Settings = Depends(settings_dep)):
    ctx = build_context(db, settings, ident)
    if ctx is None:
        return JSONResponse({"detail": "The assistant is available to registered active employees and approved admins."},
                            status_code=403)
    res = AssistantAgent(settings, DomoClient(settings), ctx).chat(body.message, [t.model_dump() for t in body.history])
    if res["status"] == "llm_unavailable" and ctx.is_employee:
        res.pop("missing_configuration", None)  # configuration details are for admins only
    code = 503 if res["status"] == "llm_unavailable" else 200
    res.pop("trace", None) if ctx.is_employee else None  # employees do not see tool internals
    return JSONResponse(res, status_code=code)
