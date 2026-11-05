import "./style.css";
import { api, setCsrf, setUnauthorizedHandler, loginUrl } from "./api.js";

const TZ = "Asia/Kolkata";
const $ = (id) => document.getElementById(id);
let offset = 0, inflight = false, chatHistory = [];

// ---------- live clock (display only; punch time is always the server's) ----------
function renderClock() {
  const now = new Date(Date.now() + offset);
  const t = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: true }).format(now);
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "long", day: "2-digit", month: "long", year: "numeric" })
    .formatToParts(now).map((x) => [x.type, x.value]));
  $("clock-time").textContent = t;
  $("clock-date").textContent = `${p.weekday}, ${p.day} ${p.month} ${p.year}`;
}
async function syncClock() {
  try {
    const t0 = Date.now();
    const j = await api("/api/attendance/time");
    offset = j.epoch_ms + (Date.now() - t0) / 2 - Date.now();
  } catch (_) { /* keep last offset */ }
  renderClock();
}

// ---------- helpers ----------
function show(view) { for (const v of ["loading", "login", "register", "main"]) $("view-" + v).hidden = v !== view; }
let msgTimer;
function message(text, kind) {
  const m = $("msg");
  m.textContent = text; m.className = "msg " + kind; m.hidden = false;
  clearTimeout(msgTimer);
  if (kind === "ok") msgTimer = setTimeout(() => (m.hidden = true), 6000);
}
setUnauthorizedHandler(() => { show("login"); setupLogin(); });

function renderToday(a) {
  $("t-in").textContent = a.punch_in;
  $("t-out").textContent = a.punch_out;
  $("t-hours").textContent = a.working_hours + (a.is_punched_in && a.elapsed ? ` (${a.elapsed} so far)` : "");
  $("t-shift").textContent = a.shift ? `${a.shift.name}  ${a.shift.start} – ${a.shift.end}` : "Not configured – contact HR";
  $("btn-in").disabled = a.is_punched_in;
  $("btn-out").disabled = !a.is_punched_in;
  renderRemarkForm((a.pending_remarks || [])[0]);
}

// ---------- sign in ----------
let devAuth = false;
function setupLogin(domain) {
  if (domain) $("domain").textContent = "@" + domain;
  $("login-btn").href = loginUrl("/");
  $("dev-form").hidden = !devAuth;
}
$("dev-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("dev-btn"); if (btn.disabled) return; btn.disabled = true;
  try {
    await api("/api/auth/dev-login", { method: "POST", body: JSON.stringify({ email: $("dev-email").value }) });
    await load();
  } catch (e) { message(e.message, "err"); }
  btn.disabled = false;
});

// ---------- remarks ----------
let currentRemark = null;
function renderRemarkForm(r) {
  currentRemark = r || null;
  $("remark-panel").hidden = !r;
  if (!r) return;
  $("rm-title").textContent = r.title;
  $("rm-message").textContent = r.message;
  $("rm-detail").textContent = r.detail;
  $("rm-prompt").firstChild.textContent = r.prompt;
  const sel = $("rm-reason");
  sel.replaceChildren(new Option("Select a reason…", ""), ...r.reasons.map((x) => new Option(x, x)));
  $("rm-text").value = "";
}
async function loadRemarks() {
  try {
    const j = await api("/api/remarks");
    const box = $("my-remarks");
    box.replaceChildren();
    if (!j.remarks.length) { box.textContent = "No remarks."; return; }
    for (const r of j.remarks) {
      const d = document.createElement("div"); d.className = "rm-item";
      d.textContent = `${r.work_date} · ${r.title} (${r.detail.replace(/\.$/, "")}) — ${r.remark_status}`;
      const s = document.createElement("small");
      s.textContent = r.reason_category ? `${r.reason_category}${r.employee_remark ? ": " + r.employee_remark : ""}` : "Awaiting your remark";
      d.append(s); box.append(d);
    }
  } catch (_) { /* non-fatal */ }
}
$("rm-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!currentRemark || !$("rm-reason").value) { message("Please choose a reason.", "err"); return; }
  const btn = $("rm-btn"); if (btn.disabled) return; btn.disabled = true;
  try {
    await api(`/api/remarks/${currentRemark.remark_id}/submit`, { method: "POST",
      body: JSON.stringify({ reason_category: $("rm-reason").value, remark: $("rm-text").value }) });
    message("Remark submitted. Thank you.", "ok");
    renderToday(await api("/api/attendance/today"));
    loadRemarks();
  } catch (e) { message(e.message, "err"); }
  btn.disabled = false;
});

// ---------- flows ----------
async function load() {
  show("loading");
  try {
    const me = await api("/api/me");
    setCsrf(me.csrf || null);
    if (me.state === "unauthenticated") { devAuth = !!me.dev_auth; setupLogin(me.domain); show("login"); }
    else if (me.state === "unregistered") { $("reg-email").textContent = me.email; $("reg-mail").value = me.email; show("register"); }
    else {
      $("d-id").textContent = me.employee.employee_id;
      $("d-name").textContent = me.employee.employee_name;
      $("d-mail").textContent = me.employee.mail_id;
      show("main");
      if (me.employee.account_status !== "ACTIVE") {
        message("Your account is awaiting HR approval or is inactive. Punching is disabled.", "err");
        $("btn-in").disabled = $("btn-out").disabled = true;
        return;
      }
      renderToday(await api("/api/attendance/today"));
      loadRemarks();
    }
  } catch (e) { show("login"); setupLogin(); message(e.message, "err"); }
}

$("reg-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("reg-btn"); if (btn.disabled) return; btn.disabled = true;
  try {
    await api("/api/register", { method: "POST", body: JSON.stringify({ employee_id: $("reg-id").value, employee_name: $("reg-name").value }) });
    message("Registration successful.", "ok");
    await load();
  } catch (e) { message(e.message, "err"); }
  btn.disabled = false;
});

let pendingKey = { IN: null, OUT: null };
async function punch(type) {
  if (inflight) return;           // blocks double taps
  inflight = true;
  const btn = $(type === "IN" ? "btn-in" : "btn-out"), other = $(type === "IN" ? "btn-out" : "btn-in");
  btn.disabled = other.disabled = true; btn.classList.add("busy");
  pendingKey[type] = pendingKey[type] || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  try {
    const body = { type, request_key: pendingKey[type] };
    const r = await api("/api/attendance/punch", { method: "POST", body: JSON.stringify(body) });
    pendingKey[type] = null;
    message(`${r.message} at ${r.time}`, "ok");
    renderToday(r.attendance);
    if (r.remark_required) { renderRemarkForm(r.remark_required); $("remark-panel").scrollIntoView({ behavior: "smooth" }); }
    loadRemarks();
  } catch (e) {
    message(e.message, "err");
    try { renderToday(await api("/api/attendance/today")); } catch (_) {}
  } finally { btn.classList.remove("busy"); inflight = false; }
}
$("btn-in").addEventListener("click", () => punch("IN"));   // one tap
$("btn-out").addEventListener("click", () => punch("OUT"));
$("signout").addEventListener("click", async () => {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) {}
  location.reload();
});

// ---------- assistant (same endpoint HR uses; role comes from the server session) ----------
function bubble(cls, text, list) {
  const d = document.createElement("div"); d.className = cls; d.textContent = text;
  if (list && list.length) { const ul = document.createElement("ul"); list.forEach((x) => { const li = document.createElement("li"); li.textContent = x; ul.append(li); }); d.append(ul); }
  $("chat-log").append(d); $("chat-log").scrollTop = $("chat-log").scrollHeight; return d;
}
$("chat-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("chat-input").value.trim(); if (!q || $("chat-btn").disabled) return;
  $("chat-input").value = ""; $("chat-btn").disabled = true;
  bubble("u", q); const wait = bubble("a", "Thinking…");
  try {
    const r = await api("/api/assistant/chat", { method: "POST", body: JSON.stringify({ message: q, history: chatHistory }) });
    const res = r.result || {};
    wait.remove();
    bubble("a", res.answer || "No answer.", res.evidence);
    chatHistory.push({ role: "user", text: q }, { role: "assistant", text: res.answer || "" });
    chatHistory = chatHistory.slice(-6);
  } catch (e) {
    wait.remove();
    bubble("a", e.payload && e.payload.status === "llm_unavailable" ? "AI analysis is currently unavailable. Please try later or contact HR." : e.message);
  }
  $("chat-btn").disabled = false;
});

renderClock(); setInterval(renderClock, 1000);
syncClock(); setInterval(syncClock, 300000);
load();
