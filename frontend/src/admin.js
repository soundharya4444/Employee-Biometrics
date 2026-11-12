import "./style.css";
import { api, rawApi, download, el, setCsrf, setUnauthorizedHandler, loginUrl } from "./api.js";

const $ = (id) => document.getElementById(id);
const view = $("view");
let role = "";
const canWrite = () => role !== "AUDITOR";

function table(cols, rows, render) {
  const t = el("table", {}, el("thead", {}, el("tr", {}, cols.map((c) => el("th", {}, c)))));
  const b = el("tbody");
  rows.forEach((r) => b.append(el("tr", {}, render(r).map((c) => el("td", {}, c ?? "")))));
  if (!rows.length) b.append(el("tr", {}, el("td", { colspan: cols.length }, "No records")));
  t.append(b);
  return el("div", { class: "tabular-wrap" }, t);
}
function notify(text, ok) {
  const m = $("msg");
  m.textContent = text; m.className = "msg " + (ok ? "ok" : "err"); m.hidden = false;
  setTimeout(() => (m.hidden = true), 8000);
}
const post = (p, body) => api(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const today = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
const daysAgo = (n) => new Date(Date.now() - n * 864e5).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
const field = (label, input) => el("label", {}, label, " ", input);
const q = (o) => Object.entries(o).filter(([, v]) => v !== "" && v != null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

// ---- plain-language rendering of assistant results (no JSON for HR) ----
const names = (arr) => (arr || []).map((x) => (x && x.employee_name ? `${x.employee_name} (${x.employee_id})` : (x && x.employee_id) || x)).join(", ");
function block(title, items) {
  return el("div", { style: "margin:10px 0" }, el("strong", {}, title), items.length
    ? el("ul", { style: "margin:4px 0 0 18px" }, items.map((t) => el("li", {}, t))) : el("p", { class: "muted", style: "margin:4px 0" }, "Nothing to report."));
}
function friendlyError(x) {
  const t = String(x || "");
  if (/not configured|unavailable/i.test(t)) return "The AI helper is not connected right now. Please ask IT to check the AI settings.";
  if (/HTTP 4\d\d|HTTP 5\d\d|request failed|timed out/i.test(t)) return "The AI helper could not be reached just now. Please try again in a minute.";
  return "Something went wrong while preparing this summary. Please try again.";
}
function showDaily(out, r) {
  const f = r.figures;
  if (!f) { out.replaceChildren(el("p", {}, friendlyError(r.error || r.message))); return; }
  const n = r.narrative;
  const box = el("div", { class: "panel" }, el("h3", { style: "margin-top:0" }, `Attendance summary for ${f.date}`));
  if (n && n.headline) box.append(el("p", {}, el("strong", {}, n.headline)));
  box.append(el("p", {}, `${f.punched_in_count} of ${f.total_active_employees} employees have punched in today; ${f.currently_working_count} are working right now.`));
  box.append(block("Not punched in yet", [names(f.not_punched_in)].filter(Boolean)));
  box.append(block("Forgot to punch out", [names(f.missing_punch_out)].filter(Boolean)));
  box.append(block("Came in late", [names(f.late_arrivals)].filter(Boolean)));
  box.append(block("Left early", [names(f.early_departures)].filter(Boolean)));
  box.append(el("p", {}, `Explanations waiting from employees: ${f.remarks_pending}. Explanations received: ${f.remarks_submitted}.`));
  if (n) {
    box.append(block("What needs attention", n.attention_items || []));
    box.append(block("Suggested next steps", n.recommendations || []));
  } else {
    box.append(el("p", { class: "muted" }, "The numbers above are exact. The written commentary is unavailable right now: " + friendlyError(r.error || r.message)));
  }
  out.replaceChildren(box);
}
function showAdvice(out, res) {
  const rec = (res.recommendations || []), esc = (res.escalations || []);
  out.replaceChildren(...(rec.length ? [block("Suggested next steps", rec)] : []), ...(esc.length ? [block("Please look at", esc)] : []));
}
function showMonitor(out, r) {
  const c = r.counts || {};
  const box = el("div", { class: "panel" }, el("h3", { style: "margin-top:0" }, "Attendance check result"));
  const quick = [
    `Forgotten punch-outs/punch-ins (last 3 days): ${c.missing_punches ?? 0}`,
    `Late or early cases (last 3 days): ${c.late_or_early_last_3_days ?? 0}`,
    `Explanations waiting from employees: ${c.pending_remarks ?? 0}`,
    `Employees often late (3+ times in 2 weeks): ${c.repeated_late_employees_14d ?? 0}`];
  if (r.status === "no_analysis_needed") box.append(el("p", {}, "All clear: nothing needs HR attention right now."));
  else if (r.status === "unchanged") box.append(el("p", {}, "Nothing new since the last check."));
  else if (r.status === "complete" && r.result && r.result.answer) {
    box.append(el("p", {}, r.result.answer));
    if ((r.result.recommendations || []).length) box.append(block("Suggested next steps", r.result.recommendations));
    if (r.result.human_review_required) box.append(el("span", { class: "tag" }, "Please review this personally"));
  } else if (r.status === "complete" && r.answer) box.append(el("p", {}, r.answer));
  else box.append(el("p", {}, friendlyError(r.error || r.message || r.status)));
  box.append(block("At a glance", quick));
  out.replaceChildren(box);
}

const tabs = {
  // ---------------- Dashboard (+ Excel downloads) ----------------
  Dashboard: async () => {
    const date = el("input", { type: "date", value: today() }), dept = el("input", { placeholder: "department" }), shift = el("input", { placeholder: "shift name" });
    const out = el("div");
    async function go() {
      const d = await api(`/admin/api/dashboard?${q({ date: date.value, department: dept.value, shift: shift.value })}`);
      const tile = (l, v) => el("div", { class: "tile" }, el("b", {}, String(v)), l);
      const bucket = (o) => Object.entries(o).map(([k, v]) => `${k}: ${v.present}/${v.total}`).join("  ·  ") || "—";
      out.replaceChildren(
        el("div", { class: "tiles" }, tile("Active employees", d.total_active_employees), tile("Present", d.punched_in.length),
          tile("Missing Punch In", d.not_punched_in.length), tile("Missing Punch Out", d.missing_punch_out.length),
          tile("Currently working", d.currently_working.length), tile("Late arrivals", d.late_arrivals.length),
          tile("Early punch outs", d.early_departures.length), tile("Pending Employee Remark", d.remarks_pending),
          tile("Remark Submitted", d.remarks_submitted)),
        el("p", {}, "By department (present/total): " + bucket(d.by_department)),
        el("p", {}, "By shift (present/total): " + bucket(d.by_shift)),
        d.shift_config_issues.length ? el("p", { class: "msg err" }, "Shift configuration issues (HR must fix): " +
          d.shift_config_issues.map((i) => `${i.employee_id} ${i.issue}`).join("; ")) : "",
        table(["ID", "Name", "Dept", "Shift", "In", "Out", "Hours", "Late (min)", "Early (min)", "Flags"], d.rows, (r) =>
          [r.employee_id, r.employee_name, r.department, r.shift || (r.shift_issue || ""), r.in, r.out, r.hours, r.late_minutes, r.early_minutes, (r.flags || []).join(", ")]));
    }
    // Excel downloads
    const from = el("input", { type: "date", value: daysAgo(6) }), to = el("input", { type: "date", value: today() });
    const run = async (btn, path) => {
      btn.disabled = true;
      try { await download(path); notify("Download started", true); } catch (e) { notify(e.message, false); }
      btn.disabled = false;
    };
    const fullBtn = el("button", { class: "btn" }, "Download full workbook (all sheets)");
    const attBtn = el("button", { class: "btn green" }, "Download attendance only");
    fullBtn.addEventListener("click", () => run(fullBtn, "/admin/api/report/download?scope=full"));
    attBtn.addEventListener("click", () => run(attBtn, `/admin/api/report/download?${q({ scope: "attendance", date_from: from.value, date_to: to.value })}`));
    view.replaceChildren(
      el("div", { class: "panel" }, el("h3", { style: "margin-top:0" }, "Download as Excel"),
        el("div", { class: "row" }, fullBtn, el("span", { class: "muted" }, "Daily Attendance, Raw Punch Logs, Monthly Summary, Shift Master, Employee Master")),
        el("div", { class: "row" }, field("Attendance from", from), field("to", to), attBtn,
          el("span", { class: "muted" }, "Only the Daily Attendance sheet, for the dates chosen"))),
      el("div", { class: "row" }, field("Date", date), field("Department", dept), field("Shift", shift), el("button", { class: "btn", onclick: go }, "Apply")), out);
    date.addEventListener("change", go);
    await go();
  },

  // ---------------- Attendance ----------------
  Attendance: async () => {
    const f = el("input", { type: "date", value: daysAgo(6) }), t = el("input", { type: "date", value: today() }),
      s = el("input", { placeholder: "name / id / email" }), dept = el("input", { placeholder: "department" }), shift = el("input", { placeholder: "shift name" });
    const out = el("div");
    async function go() {
      const d = await api(`/admin/api/attendance?${q({ date_from: f.value, date_to: t.value, employee: s.value, department: dept.value, shift: shift.value })}`);
      out.replaceChildren(table(["Date", "ID", "Name", "Dept", "Shift", "In", "Out", "Hours", "Sessions", "Flags"], d.rows, (r) =>
        [r.date, r.employee_id, r.employee_name, r.department, r.shift, r.in, r.out, r.hours, r.sessions, (r.flags || []).join(", ") + (r.open ? " OPEN" : "")]));
    }
    view.replaceChildren(el("div", { class: "row" }, field("From", f), field("To", t), field("Employee", s), field("Dept", dept), field("Shift", shift),
      el("button", { class: "btn", onclick: go }, "Search")), out);
    await go();
  },

  // ---------------- Monthly ----------------
  Monthly: async () => {
    const m = el("input", { type: "month", value: today().slice(0, 7) });
    const out = el("div");
    async function go() {
      const d = await api("/admin/api/monthly?month=" + m.value);
      out.replaceChildren(table(["ID", "Name", "Shift", "Present", "Absent", "Hours", "Late", "Early", "Pending remarks", "No OUT", "No IN", "Status"], d.employees, (r) =>
        [r.employee_id, r.employee_name, r.shift, r.present, r.absent, r.total_hours, r.late, r.early, r.pending_remarks, r.missing_out, r.missing_in, r.status]));
    }
    view.replaceChildren(el("div", { class: "row" }, field("Month", m), el("button", { class: "btn", onclick: go }, "Load")), out);
    await go();
  },

  // ---------------- Remarks ----------------
  Remarks: async () => {
    const st = el("select", {}, ["", "Pending Employee Remark", "Remark Submitted"].map((s) => el("option", { value: s }, s || "ALL")));
    const kind = el("select", {}, ["", "LATE_ARRIVAL", "EARLY_DEPARTURE"].map((s) => el("option", { value: s }, s || "ALL types")));
    const emp = el("input", { placeholder: "employee" }), out = el("div");
    async function go() {
      const d = await api(`/admin/api/remarks?${q({ status: st.value, kind: kind.value, employee: emp.value })}`);
      out.replaceChildren(table(["Date", "Employee", "Type", "Shift", "Scheduled", "Actual", "Variance (min)", "Status", "Reason", "Employee remark"], d.remarks, (r) =>
        [r.date, `${r.employee_id} ${r.employee_name}`, r.type, r.shift, r.scheduled, r.actual, r.variance_minutes, r.remark_status, r.reason_category, r.employee_remark]));
    }
    view.replaceChildren(el("div", { class: "row" }, field("Status", st), field("Type", kind), field("Employee", emp), el("button", { class: "btn", onclick: go }, "Filter")), out);
    await go();
  },

  // ---------------- Employees ----------------
  Employees: async () => {
    const d = await api("/admin/api/employees");
    const flip = async (e) => {
      try { await post(`/admin/api/employees/${e.employee_id}/status`, { status: e.status === "ACTIVE" ? "INACTIVE" : "ACTIVE" }); tabs.Employees(); } catch (x) { notify(x.message, false); }
    };
    view.replaceChildren(table(["ID", "Name", "Email", "Dept", "Status", "Registered", "Shift", "Shift check", ""], d.employees, (e) =>
      [e.employee_id, e.employee_name, e.email, e.department, e.status, e.registered ? "Yes" : "No", e.shift, e.shift_issue || "OK",
        canWrite() ? el("button", { class: "btn", onclick: () => flip(e) }, e.status === "ACTIVE" ? "Deactivate" : "Activate") : ""]));
  },

  // ---------------- Assistant ----------------
  Assistant: async () => {
    const [st, latest] = await Promise.all([api("/admin/api/agent/status"), api("/admin/api/agent/monitor/latest")]);
    const lm = latest.latest;
    const monitorBox = el("div", { class: "panel" }, el("h3", { style: "margin-top:0" }, "Latest automatic check"),
      lm ? el("div", {}, el("p", { class: "muted" }, `${lm.when.replace("T", " ").slice(0, 19)} UTC · ${lm.status}`),
        el("p", {}, lm.answer || lm.error || "No analysis was needed (no new findings)."),
        lm.human_review_required ? el("span", { class: "tag" }, "Human review recommended") : "")
        : el("p", { class: "muted" }, "No monitoring run yet."));
    const log = el("div", { class: "chat-log" }), box = el("input", { size: 70, placeholder: "e.g. Who has missing punch-outs this week? Investigate the latest late remark of 210145." });
    let hist = [];
    const out = el("div");
    const ask = async () => {
      const text = box.value.trim(); if (!text) return; box.value = "";
      log.append(el("div", { class: "u" }, text)); const w = el("div", { class: "a" }, "Working…"); log.append(w);
      const { r, j } = await rawApi("/api/assistant/chat", { method: "POST", body: JSON.stringify({ message: text, history: hist }) });
      const res = j.result || {};
      w.textContent = r.ok ? (res.answer || "No answer") : (j.message || j.detail || j.error || "Error");
      if (res.evidence && res.evidence.length) w.append(el("ul", {}, res.evidence.map((x) => el("li", {}, x))));
      if (res.human_review_required) w.append(el("div", { class: "tag" }, "Human review recommended"));
      showAdvice(out, res);
      hist = [...hist, { role: "user", text }, { role: "assistant", text: res.answer || "" }].slice(-6);
    };
    box.addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
    view.replaceChildren(
      el("p", {}, st.configured ? "AI helper: connected" : "AI helper: not connected. Please ask IT to set it up (missing: " + st.missing.join(", ") + ")"),
      el("p", { class: "muted" }, `Automatic checks: ${st.monitor_enabled ? "run every " + st.monitor_interval_minutes + " minutes" : "turned off"}`),
      monitorBox, log, el("div", { class: "row" }, box, el("button", { class: "btn", onclick: ask }, "Ask")),
      canWrite() ? el("div", { class: "row" },
        el("button", { class: "btn", onclick: async () => { out.replaceChildren(el("p", {}, "Preparing today's summary… this can take up to a minute.")); try { showDaily(out, await post("/admin/api/agent/daily-summary")); } catch (e) { out.replaceChildren(el("p", {}, friendlyError(e.message))); } } }, "Today's attendance summary"),
        el("button", { class: "btn", onclick: async () => { out.replaceChildren(el("p", {}, "Checking attendance… this can take up to a minute.")); try { showMonitor(out, await post("/admin/api/agent/monitor/run")); } catch (e) { e.payload && e.payload.counts ? showMonitor(out, e.payload) : out.replaceChildren(el("p", {}, friendlyError(e.message))); } } }, "Check for problems now")) : "",
      el("p", { class: "muted" }, "Today's attendance summary: who is in, late, left early or forgot to punch out. Check for problems now: looks at the last few days for missed punches and unanswered late/early explanations."),
      out);
  },
};

// ---------------- boot: sign-in -> admin check -> tabs ----------------
let pocAuth = false;
function showLogin(me) {
  $("tabs").hidden = $("who").hidden = $("denied").hidden = true;
  $("login").hidden = false;
  $("login-btn").href = loginUrl("/admin/");
  if (me && "poc_auth" in me) pocAuth = !!me.poc_auth;
  $("dev-form").hidden = !(pocAuth || (me && me.dev_auth));
  $("dev-empid").hidden = !pocAuth; $("dev-empid").required = pocAuth;
  if (pocAuth) $("login-btn").hidden = true;
}
async function signOut() {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) {}
  location.reload();
}
$("logout").addEventListener("click", (e) => { e.preventDefault(); signOut(); });
$("denied-out").addEventListener("click", signOut);
$("dev-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try { await api(pocAuth ? "/api/auth/poc-login" : "/api/auth/dev-login", { method: "POST", body: JSON.stringify(pocAuth ? { email: $("dev-email").value, employee_id: $("dev-empid").value } : { email: $("dev-email").value }) }); boot(); }
  catch (e) { notify(e.message, false); }
});
setUnauthorizedHandler(() => showLogin({}));

async function boot() {
  try {
    const me = await api("/api/me");
    if (me.state === "unauthenticated") { showLogin(me); return; }
    setCsrf(me.csrf);
    let adm;
    try { adm = await api("/admin/api/me"); }
    catch (e) {
      if (e.status === 403) { $("login").hidden = true; $("denied-email").textContent = me.email; $("denied").hidden = false; return; }
      if (e.status === 401) { showLogin(me); return; }
      throw e;
    }
    role = adm.role; setCsrf(adm.csrf);
    $("login").hidden = $("denied").hidden = true;
    $("who-email").textContent = adm.email; $("role").textContent = adm.role;
    $("who").hidden = $("tabs").hidden = false;
    const nav = $("tabs");
    nav.replaceChildren();
    Object.keys(tabs).forEach((name) => {
      const b = el("button", { onclick: async () => {
        nav.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        try { await tabs[name](); } catch (e) { notify(e.message, false); }
      } }, name);
      nav.append(b);
    });
    nav.firstChild.click();
  } catch (e) { notify(e.message, false); }
}
boot();
