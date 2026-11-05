"""Proactive attendance monitoring (Python scheduler). Python scans deterministically; the LLM agent is only
invoked when there are NEW findings that need analysis - never continuously and never on page loads."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import tools
from app.agents.domo_agent import AssistantAgent
from app.agents.domo_client import DomoClient
from app.config import Settings, get_settings
from app.database.models import AttendanceRemark, AuditLog, PENDING_REMARK, REMARK_SUBMITTED
from app.services import attendance_service as att
from app.services.audit import audit

log = logging.getLogger("attendance.monitor")
KIND = "AGENT_MONITOR_RUN"


def scan(db: Session, settings: Settings) -> dict:
    ctx = tools.ToolContext(db, settings, "system", "SYSTEM")
    today = att.now_utc().astimezone(settings.tz).date()
    d3, d14 = today - timedelta(days=2), today - timedelta(days=13)
    missing = tools.detect_missing_punches(ctx, {"date_from": d3, "date_to": today})["items"]
    ex = att.exception_scan(db, settings, d3, today)
    late14 = [r for r in att.exception_scan(db, settings, d14, today) if r["kind"] == "LATE_ARRIVAL"]
    counts: dict[str, int] = {}
    for r in late14:
        counts[r["employee_id"]] = counts.get(r["employee_id"], 0) + 1
    repeated = sorted(e for e, n in counts.items() if n >= 3)
    pending = db.scalars(select(AttendanceRemark).where(AttendanceRemark.remark_status == PENDING_REMARK)).all()
    submitted = db.scalars(select(AttendanceRemark).where(AttendanceRemark.remark_status == REMARK_SUBMITTED,
                                                          AttendanceRemark.created_at >= att.now_utc() - timedelta(days=3))).all()
    recent_submitted = sorted(r.remark_id for r in submitted)
    return {
        "missing_punches": [{"employee_id": m["employee_id"], "work_date": m["work_date"], "type": m["type"]}
                            for m in missing if m["type"] != "OPEN_SESSION"],
        "late_or_early_last_3_days": [{"employee_id": r["employee_id"], "date": r["work_date"], "kind": r["kind"],
                                       "minutes": r["variance_minutes"], "remark_status": r["remark_status"]} for r in ex],
        "pending_remarks": sorted(r.remark_id for r in pending),
        "recently_submitted_remarks": recent_submitted,
        "repeated_late_employees_14d": repeated,
        "shift_config_issues": att.shift_config_issues(db),
    }


def _needs_analysis(f: dict) -> bool:
    return bool(f["missing_punches"] or f["recently_submitted_remarks"] or f["repeated_late_employees_14d"])


def _fingerprint(f: dict) -> str:
    key = {k: f[k] for k in ("missing_punches", "recently_submitted_remarks", "repeated_late_employees_14d")}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:20]


def run_cycle(db: Session, settings: Settings, provider=None, force: bool = False, actor: str = "system") -> dict:
    provider = provider or DomoClient(settings)
    f = scan(db, settings)
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in f.items()}
    if not _needs_analysis(f):
        audit(db, "monitor", KIND, "agent", None, {"status": "no_analysis_needed", "counts": summary})
        db.commit()
        return {"status": "no_analysis_needed", "counts": summary, "findings": f}
    fp = _fingerprint(f)
    last = db.scalar(select(AuditLog).where(AuditLog.action == KIND, AuditLog.actor.like("agent:%") | (AuditLog.actor == "monitor"))
                     .order_by(AuditLog.audit_id.desc()))
    last_meta = (last.meta or {}) if last else {}
    if not force and last_meta.get("fingerprint") == fp:
        same_state = last_meta.get("status") == "complete" or (
            last_meta.get("status") == "llm_unavailable" and not provider.is_configured)
        if same_state:
            return {"status": "unchanged", "counts": summary}
    if not provider.is_configured:
        audit(db, "monitor", KIND, "agent", None, {"status": "llm_unavailable", "fingerprint": fp, "counts": summary,
                                                   "missing_configuration": provider.missing_config()})
        db.commit()
        return {"status": "llm_unavailable", "counts": summary, "findings": f,
                "message": "AI analysis is unavailable (Domo LLM not configured). Deterministic findings are listed."}
    ctx = tools.ToolContext(db, settings, actor, "SYSTEM")
    objective = ("Scheduled attendance review. Deterministic findings follow (ids only). Investigate what needs it with "
                 "tools and give a concise HR summary with recommended follow-up.\nFINDINGS: " + json.dumps(f, default=str)[:5000])
    out = AssistantAgent(settings, provider, ctx).chat(objective, kind=KIND, extra={"fingerprint": fp, "counts": summary})
    return out


class MonitorWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, settings: Settings | None = None):
        s = settings or get_settings()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(s,), name="attendance-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self, s: Settings):
        from app.database.connection import SessionLocal

        while not self._stop.wait(max(1, s.monitor_interval_minutes) * 60):
            try:
                with SessionLocal() as db:
                    run_cycle(db, s)
            except Exception:  # noqa: BLE001
                log.exception("monitor cycle failed")


monitor_worker = MonitorWorker()
