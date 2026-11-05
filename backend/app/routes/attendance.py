from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import csrf_protect, rate_limit, require_employee, settings_dep
from app.config import Settings
from app.database.connection import get_db
from app.database.models import Employee
from app.services import attendance_service as att
from app.services import remark_service
from app.services.excel_generator import worker
from app.services.client_ip import get_client_ip

router = APIRouter(prefix="/api", tags=["attendance"])


class PunchIn(BaseModel):
    type: str = Field(pattern="^(IN|OUT)$")
    request_key: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class RemarkIn(BaseModel):
    reason_category: str = Field(max_length=100)
    remark: str | None = Field(default=None, max_length=1000)


@router.get("/attendance/time")
def server_time(settings: Settings = Depends(settings_dep)):
    """Server clock used to sync the on-page clock. The punch timestamp is always taken server-side."""
    return {"epoch_ms": int(att.now_utc().timestamp() * 1000), "timezone": settings.timezone}


@router.get("/attendance/today")
def today(emp: Employee = Depends(require_employee), db: Session = Depends(get_db),
          settings: Settings = Depends(settings_dep)):
    v = att.today_view(db, settings, emp)
    v["pending_remarks"] = remark_service.list_own(db, settings, emp, pending_only=True)
    return v


@router.get("/attendance/history")
def history(days: int = Query(14, ge=1, le=62), emp: Employee = Depends(require_employee),
            db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    return {"history": att.history_view(db, settings, emp, days)}  # only the caller's own records


@router.post("/attendance/punch", dependencies=[Depends(csrf_protect), Depends(rate_limit("punch", "punch_rate_limit"))])
def punch(body: PunchIn, request: Request, emp: Employee = Depends(require_employee),
          db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    try:
        ev, remark = att.record_punch(db, settings, emp, body.type, client_ip=get_client_ip(request, settings),
                                      request_key=body.request_key)
    except att.PunchError as e:
        return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.http_status)
    worker.request_update()  # after the DB commit
    view = att.today_view(db, settings, emp)
    pending = remark_service.list_own(db, settings, emp, pending_only=True)
    view["pending_remarks"] = pending
    return {"message": "Punched in successfully" if ev.event_type == "IN" else "Punched out successfully",
            "time": att.fmt_time(ev.event_timestamp, settings), "attendance": view,
            "remark_required": remark_service.remark_view(remark, settings) if remark else None}


@router.get("/remarks")
def my_remarks(emp: Employee = Depends(require_employee), db: Session = Depends(get_db),
               settings: Settings = Depends(settings_dep)):
    return {"remarks": remark_service.list_own(db, settings, emp)}


@router.post("/remarks/{remark_id}/submit", dependencies=[Depends(csrf_protect), Depends(rate_limit("remark", "punch_rate_limit"))])
def submit_remark(remark_id: int, body: RemarkIn, emp: Employee = Depends(require_employee),
                  db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    try:
        r = remark_service.submit_remark(db, settings, emp, remark_id, body.reason_category, body.remark)
    except remark_service.RemarkError as e:
        return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.http_status)
    return {"remark": remark_service.remark_view(r, settings)}

