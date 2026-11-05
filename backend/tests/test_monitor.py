import json

from sqlalchemy import select

from app.agents.domo_client import DomoClient
from app.database.models import AttendanceEvent, AuditLog, Employee
from app.services import monitor
from conftest import add_master, add_shift, ist


class FakeLLM:
    is_configured = True

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def missing_config(self):
        return []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def seed_missing_out(db, settings):
    add_shift(db)
    add_master(db, settings, shift="General")
    e = db.scalar(select(Employee))
    db.add(AttendanceEvent(employee_id=e.id, event_type="IN", event_timestamp=ist(2026, 11, 4, 9, 0), source="QR",
                           network_verified=True, recorded_by="t"))
    db.commit()


def test_llm_not_invoked_when_nothing_to_analyse(db, settings, freeze):
    freeze(2026, 11, 4, 12, 0)
    add_shift(db)
    add_master(db, settings, shift="General")
    llm = FakeLLM([])                                            # any call would raise IndexError
    out = monitor.run_cycle(db, settings, llm)
    assert out["status"] == "no_analysis_needed" and llm.prompts == []


def test_cycle_invokes_agent_once_per_new_findings(db, settings, freeze):
    freeze(2026, 11, 6, 10, 0)
    seed_missing_out(db, settings)
    llm = FakeLLM([json.dumps({"action": "tool", "tool": "get_employee_shift", "arguments": {"employee_id": "EMP001"}}),
                   json.dumps({"action": "final", "result": {"answer": "1 missing punch out (EMP001, 04-Nov).",
                                                             "escalations": ["EMP001"]}})])
    out = monitor.run_cycle(db, settings, llm)
    assert out["status"] == "complete" and "EMP001" in llm.prompts[0] and "SCHEDULED MONITOR" in llm.prompts[0]
    run = db.scalar(select(AuditLog).where(AuditLog.action == "AGENT_MONITOR_RUN"))
    assert run.meta["fingerprint"] and run.actor == "agent:system" and run.meta["role"] == "SYSTEM"
    assert run.meta["tools"] == ["get_employee_shift"] and "EMP001" in run.meta["answer"]
    again = monitor.run_cycle(db, settings, FakeLLM([]))        # same findings -> LLM not called again
    assert again["status"] == "unchanged"


def test_monitor_system_role_cannot_send_or_regenerate(db, settings):
    from app.agents.tools import ToolContext, call_tool, tool_catalog
    ctx = ToolContext(db, settings, "system", "SYSTEM")
    assert not call_tool(ctx, "generate_attendance_report", {})["ok"]
    names = {t["name"] for t in tool_catalog("SYSTEM")}
    assert "generate_attendance_report" not in names and "detect_missing_punches" in names    # read-only monitor


def test_monitor_reports_unavailable_when_domo_missing(db, settings, freeze):
    freeze(2026, 11, 6, 10, 0)
    seed_missing_out(db, settings)
    out = monitor.run_cycle(db, settings, DomoClient(settings))
    assert out["status"] == "llm_unavailable" and out["findings"]["missing_punches"] and "unavailable" in out["message"]
    again = monitor.run_cycle(db, settings, DomoClient(settings))   # no audit spam while still unconfigured
    assert again["status"] == "unchanged"


def test_scan_lists_pending_remarks_and_config_issues(shifted, db, settings, freeze):
    from conftest import punch
    c, csrf = shifted
    freeze(2026, 11, 4, 9, 45)
    punch(c, csrf, "IN")
    add_master(db, settings, "EMP002", "Jane", "jane@gwcdata.ai")
    f = monitor.scan(db, settings)
    assert len(f["pending_remarks"]) == 1 and f["shift_config_issues"][0]["employee_id"] == "EMP002"
    assert f["late_or_early_last_3_days"][0]["remark_status"] == "Pending Employee Remark"
    assert monitor._needs_analysis(f) is False                  # a pending remark alone is the employee's action, not an LLM job
