from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import csrf_protect, current_identity, rate_limit, require_identity, settings_dep
from app.config import Settings
from app.database.connection import get_db
from app.services.employee_service import RegistrationError, employee_by_identity, employee_public, register_employee

router = APIRouter()


class RegisterIn(BaseModel):
    employee_id: str = Field(max_length=32)
    employee_name: str = Field(max_length=200)


@router.get("/api/me")
def me(request: Request, ident: dict | None = Depends(current_identity), db: Session = Depends(get_db),
       settings: Settings = Depends(settings_dep)):
    if not ident:
        return {"state": "unauthenticated", "domain": settings.allowed_email_domain,
                "dev_auth": settings.dev_auth_enabled}
    emp = employee_by_identity(db, ident.get("sub"), ident["email"])
    base = {"email": ident["email"], "csrf": ident["csrf"], "domain": settings.allowed_email_domain,
            "dev_auth": settings.dev_auth_enabled}
    if not emp:
        return {**base, "state": "unregistered", "name": ident.get("name")}
    return {**base, "state": "registered", "employee": employee_public(emp)}


@router.post("/api/register", dependencies=[Depends(csrf_protect), Depends(rate_limit("register", "auth_rate_limit"))])
def register(body: RegisterIn, ident: dict = Depends(require_identity), db: Session = Depends(get_db),
             settings: Settings = Depends(settings_dep)):
    try:
        emp = register_employee(db, settings, sub=ident.get("sub"), email=ident["email"],
                                employee_id=body.employee_id, employee_name=body.employee_name)
    except RegistrationError as e:
        return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.http_status)
    return {"employee": employee_public(emp)}
