# GWC Data.AI â€“ Agentic Employee Attendance System

**Two separate projects:** `backend/` = FastAPI backend (API only, no HTML) Â· `frontend/` = Vite static site (employee page +
admin portal) built with `npm run build` and deployed to Vercel. Permanent QR â†’ company-authenticated punch in/out â†’ database as source of truth â†’ shift-aware
late/early remarks â†’ generated Excel workbook (HR maintains Shift Master / Employee Master in it) â†’ admin-only portal â†’
**one combined Employee + HR assistant** driven by the Domo LLM through 12 audited tools, plus scheduled monitoring.

## Architecture in one paragraph
Python owns everything critical: authentication (Google Workspace OIDC, `@gwcdata.ai` enforced server-side), punch
validation, shift/tolerance maths, DB transactions, Excel generation and access control.
The LLM never touches punch records or remark statuses; it can only call the tools in `app/agents/tools.py`, whose
availability depends on the **session-derived role**. `attendance_events` is append-only (ORM guard + DB triggers).

```
app/                      BACKEND (Python, API only)
  main.py config.py       (CORS, cookie settings, no static files)
  auth/        deps.py (session, CSRF, rate-limit, RBAC)   routes.py (Google OIDC, dev login JSON)
  routes/      employee.py attendance.py (punch, remarks) assistant.py (ONE chat endpoint) admin.py
  services/    attendance_service.py shift_service.py remark_service.py client_ip.py
               excel_generator.py hr_import.py monitor.py employee_service.py audit.py
  database/    models.py connection.py migrations/ (Alembic)
  agents/      domo_client.py (provider adapter)  domo_agent.py (agent loop)  tools.py (12 tools)  prompts.py
frontend/                 FRONTEND (Vite, plain JS) -> `npm run build` -> dist/
  index.html              employee page            admin/index.html   admin portal
  src/api.js employee.js admin.js style.css        vercel.json  .env.example
scripts/       generate_qr.py import_employees.py import_hr_workbook.py generate_excel.py seed_dev_data.py (dev only)
storage/       reports/ (NOT web-served)    tests/ (backend tests)
```

## Windows PowerShell setup (Python 3.13)
```powershell
cd "C:\Users\<you>\Documents\QR-Based Employee Attendance System"
cd backend\npy -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
Copy-Item .env.example .env           # then edit .env
```

## Local development
Minimal `.env` (no Google credentials needed):
```
SECRET_KEY=<random 32+ chars>
DEV_AUTH_ENABLED=true          # DEV-ONLY sign-in: accepts any @gwcdata.ai address WITHOUT verification
NETWORK_CHECK_ENABLED=false
BOOTSTRAP_ADMIN_EMAILS=you@gwcdata.ai
```
Backend `.env` also needs `FRONTEND_URL` â€“ see `.env.example`.
```powershell
python scripts\import_employees.py employees.csv     # employee master list (columns in script docstring)
python scripts\seed_dev_data.py                       # optional DEV-ONLY demo employees + SAMPLE shifts
.\start_server.ps1                                    # (in backend\) backend  -> http://<this-pc>:8000   (API only)
cd ..\frontend; .\start_frontend.ps1                  # frontend -> http://<this-pc>:4173   (build + serve; 2nd window)
pytest                                                # backend tests, in-memory SQLite
```
Frontend only, with hot reload: `cd frontend; npm install; npm run dev` (http://localhost:5173, API from `frontend\.env`).
Backend API docs (dev only): `/docs`. The backend serves no HTML: open the **frontend** address (the one in the QR).

### Admin portal (6 pages): Dashboard Â· Attendance Â· Monthly Â· Remarks Â· Employees Â· Assistant
* **Dashboard** also has the Excel downloads: **full workbook** (all 5 sheets) or **attendance only** (just the Daily
  Attendance sheet, for a chosen From/To date range). Both are admin-only and audited.
* **Removed with their pages** (code, API and database tables): admin-assisted corrections, day status (WFH/leave/holiday/approved),
  HR review cases, AI notification drafts/SMTP, the audit-log viewer, workbook upload/regenerate/status endpoints. Audit
  *recording* stays (table `audit_logs`, read it in the database). Shifts are applied from the command line, below.

### Who is an admin
`BOOTSTRAP_ADMIN_EMAILS` in `.env` (comma separated) **adds** admins when the backend starts. It never removes anyone: to remove an
admin, delete the row from the `admin_users` table (or set its `status` to `DISABLED`) â€“ the change applies on their next request.

### Shifts: how HR provides them (no shift is built into the code)
1. Dashboard â†’ **Download full workbook**.
2. Edit the yellow sheets: **Shift Master** (`Shift Name | Start Time | End Time | Late Tolerance Minutes |
   Early Departure Tolerance Minutes | Active`) and **Employee Master** (the `Shift` column must match a Shift Master name).
   End â‰¤ Start means overnight (e.g. 10:00 PM â†’ 06:00 AM). Times as `hh:mm AM/PM`.
3. Apply it: `python scripts\import_hr_workbook.py edited.xlsx` (or save it in place as `storage\reports\attendance.xlsx` on the
   server). The edit is validated as a whole, applied to the DB, then the workbook is rebuilt â€“ **HR values are never
   overwritten**. Invalid edits change nothing and are listed (in-place edits are kept as `attendance.rejected-<time>.xlsx`).
   Only those two sheets are read; attendance sheets are generated output.
Employees without a valid mapping are flagged (dashboard, Employee Master "Shift Check", assistant) and never given a
guessed schedule â€“ no remark is requested for them.

## Employee flow
1. Scan the QR â†’ company Google sign-in (`hd=gwcdata.ai`; backend re-checks `email_verified`, domain and `hd`).
2. First time only: Employee ID (+name) is linked to the verified Google subject. With `REQUIRE_MASTER_LIST=true` the ID
   **and e-mail** must match the imported list. With it `false`, registrations are `PENDING_APPROVAL` until HR activates.
3. Returning (same or new device): sign-in resolves the existing profile â€“ never a second registration. Session cookie is
   HTTP-only/Secure/SameSite, expires after `SESSION_MAX_AGE_SECONDS`; after expiry only sign-in is needed.
4. Punch times are the server clock (UTC in DB, Asia/Kolkata on screen). Double-click/replay protection: UI lock +
   idempotency key + row lock + min gap + open-session rules.

### Late arrival / early punch-out remarks
Python compares the punch with the assigned shift and that shift's tolerance (from Shift Master):
* **Late**: first IN of the day more than `late tolerance` minutes after shift start. **Early**: any OUT more than
  `early tolerance` minutes before shift end (overnight-aware). Within tolerance â†’ nothing is asked.
* The remark request is created in the same transaction as the punch, with the exact wording/reasons from the brief.
* `remark_status` is exactly **`Pending Employee Remark`** â†’ **`Remark Submitted`** (DB CHECK constraint; only the employee's
  own submission changes it; it is final). The AI never changes it; its findings are shown to HR in the Assistant page, never stored in the remark status.
* Policy note: because the system cannot know whether a punch-out is the day's last, a break punch-out before shift end
  also asks for a remark (choose "Work-related reason"/"Other"). A request is not a finding of fault.

### Punching from any network
There is **no office-network rule** and **no work-mode question** (the WFO / WFH / WFCO choice was removed for now): employees can
punch from the office Wi-Fi, home Wi-Fi or mobile data, and Punch In / Punch Out is a single tap.
* The client IP is still **recorded** (audit log + Raw Punch Logs) and used for rate limiting; `X-Forwarded-For` is honoured only
  from `TRUSTED_PROXIES`. No GPS. The `network_verified` and `work_mode` columns in `attendance_events` are legacy/unused
  (the table is append-only, so they were left in place with their defaults).

### Missing punches
A missing Punch Out is never invented. An open session older than `STALE_SESSION_HOURS` stays open (logged as
`STALE_SESSION_LEFT_OPEN`) and a new Punch In is allowed; reports and the assistant list it as a missing Punch Out. There is no
correction workflow: HR follows up with the employee outside the system.

## Excel workbook (`storage/reports/attendance.xlsx`, admins only)
Sheets: **Daily Attendance** (`S.No | Employee Name | Shift | date: In Time | Out Time â€¦`, merged date headers,
`hh:mm AM/PM`), **Raw Punch Logs**, **Monthly Summary**, **Shift Master**, **Employee Master**.
Summary policy for several sessions in a day: first IN / last OUT (blank while open); everything is in Raw Punch Logs.
Overnight OUT appears under the IN's date (yellow). There is no leave/holiday/WFH day-status feature: "Absent" = a working
weekday (`WORKING_DAYS`) in the past with no punch.
Updates: after the DB commit a single report worker (coalesced) regenerates atomically under a file lock; errors are
recorded (`REPORT_ERROR` audit) and retried automatically every 45 s (e.g. while the file is open in Excel).

## The assistant (one for everyone) and Domo
* One endpoint `/api/assistant/chat`. **Role and identity come from the session** (approved admin â†’ HR tools; registered
  employee â†’ self-only tools). Employee-supplied ids/roles in chat or request bodies are ignored; employee tool calls that pass
  `employee_id` are refused; employees cannot see org-wide, investigative or mutating tools, nor tool internals.
* The model decides which tools to call, reads results, calls more, and returns `{answer, evidence, recommendations,
  escalations, human_review_required}`. There is no fixed questionâ†’tool mapping. Each run and each tool call is audited
  (`AGENT_RUN`, `AGENT_TOOL_CALL` in `audit_logs`).
* 12 tools: `get_employee_details, get_employee_shift, get_attendance_records, get_employee_remarks, get_attendance_policy,
  detect_missing_punches, detect_late_arrivals, detect_early_departures, investigate_attendance_exception,
  analyse_attendance_pattern, generate_daily_attendance_summary, generate_attendance_report`. Read-only except workbook
  regeneration (HR admins only). The assistant cannot open cases or send messages; it recommends, and a human acts.
  Verified evidence and the employee's unverified statement are returned separately; the policy tool quotes only system
  rules and HR-supplied text (`ATTENDANCE_POLICY_FILE`).
* **Monitoring**: `MONITOR_ENABLED=true` runs a Python scheduler every `MONITOR_INTERVAL_MINUTES`; it scans deterministically
  and calls the LLM **only** for new findings (missing punches, recently submitted remarks, repeated lateness), once per
  fingerprint, read-only. The Assistant page shows the latest summary; *Run monitor now* forces a run. Never on page loads.
* **Domo status â€“ NOT verified against a live instance.** The adapter assumes no endpoint, model or SDK; everything is env
  config (`DOMO_*`). It talks to a text-generation endpoint using a JSON tool-call protocol, because no native Domo
  agent/tool-calling API could be confirmed. If Domo is unconfigured, every AI feature answers **"AI analysis is unavailable"**
  (HTTP 503) and nothing is mocked; deterministic dashboards and the daily-summary *figures* still work.
  **Needed from you:** `DOMO_INSTANCE_URL`, a credential allowed to call the AI service, `DOMO_LLM_ENDPOINT_PATH` (+ model
  name, request/response field names if they differ), and confirmation AI services are enabled for the account.
  Tests use a scripted fake provider to test the loop, tools and guardrails; that proves the code, not Domo.

## Production deployment

### A. Frontend on Vercel (`npm run build`)
1. Push the repo; in Vercel **Add New â†’ Project**, set **Root Directory = `frontend`**. Framework preset *Vite*; build command
   `npm run build`, output `dist` (already in `frontend/vercel.json`).
2. Environment variable **`VITE_API_BASE_URL`** = the public **HTTPS** URL of the backend (no trailing slash). It is baked in at build
   time, so redeploy after changing it. Locally: `cd frontend; npm install; npm run build` produces `frontend/dist`.
3. The QR code points to the Vercel address: `python scripts\generate_qr.py --url https://<your-vercel-or-custom-domain>`.

### B. Backend must be reachable from the internet over HTTPS
Vercel hosts only the static frontend. A browser on an `https://` page **cannot call an `http://192.168â€¦` API (mixed content)**, so the
LAN-only setup cannot be used with Vercel: host the backend on a server with an HTTPS URL (or an HTTPS tunnel to your PC).
* **Cookies across two sites:** the session is an HTTP-only cookie. Best: put both on one registrable domain, e.g. frontend
  `attendance.gwcdata.ai` (Vercel custom domain) and API `api-attendance.gwcdata.ai`, with `COOKIE_DOMAIN=.gwcdata.ai`,
  `COOKIE_SAMESITE=lax`. If they are on different sites (e.g. `*.vercel.app` + another host) use `COOKIE_SAMESITE=none` +
  `COOKIE_SECURE=true`, **but Safari/iPhone blocks such third-party cookies, so sign-in can fail on iPhones**.
* **Any network works:** nothing depends on the office network, so employees on mobile data can punch. If a proxy/tunnel sits
  in front of the API, list it in `TRUSTED_PROXIES` so the recorded client IP is the real one.
* Backend env: `FRONTEND_URL=https://<frontend>` (CORS + post-login redirect), `BASE_URL=https://<api>` (Google callback
  `https://<api>/auth/callback`), plus the items below.

### C. Backend settings
1. PostgreSQL 15+; `DATABASE_URL=postgresql://user:pass@host:5432/attendance`.
2. `.env`/secret store: `APP_ENV=production`, strong `SECRET_KEY`, `COOKIE_SECURE=true`, `BASE_URL`, `FRONTEND_URL`, Google client id/secret
   (Web client, consent screen *Internal*), `TRUSTED_PROXIES`,
   `BOOTSTRAP_ADMIN_EMAILS`, `DOMO_*`, `MONITOR_ENABLED=true`.
   The app refuses to start in production with dev auth, weak secret, insecure cookies, SQLite, or an http:// `FRONTEND_URL`.
3. `alembic upgrade head` (creates tables **and** the append-only triggers; the Postgres trigger SQL is untested here).
4. Behind an HTTPS reverse proxy: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers` and
   `TRUSTED_PROXIES=127.0.0.1/32`; the proxy must overwrite `X-Forwarded-For`.
5. Run **one** app process, or accept: rate limiter is per-process, and each worker would run its own monitor
   (use a single scheduler instance when scaling out). Workbook writes are file-locked.
6. Import employees (`scripts\import_employees.py`), apply the HR shift workbook (`scripts\import_hr_workbook.py`), generate/print the QR.
7. `storage/reports` readable only by the service account; never mounted by the web server.

### D. Deploying on Render (only `backend/` and `frontend/` are deployed)
`storage/` is **not** deployed: it holds local dev data (`dev.db`, `employees.csv`, `qr/`, `reports/`) and is gitignored.
Render's filesystem is wiped on every deploy, so production uses:
* **Database:** Render PostgreSQL (`DATABASE_URL`), never `dev.db`.
* **Reports:** a Render **Persistent Disk** (paid web service plan) mounted at `/var/data`, with `REPORTS_DIR=/var/data/reports`.
  The folder and `attendance.xlsx` are created by the app on first run.
* **QR image / employees.csv:** generate/import locally; not needed on the server.

Steps: push the repo, Render > New > Blueprint (uses `render.yaml`, which sets `rootDir: backend`, runs `alembic upgrade head`
before each deploy, health check `/healthz`). Then fill the `sync: false` env vars in the dashboard (`BASE_URL`, `FRONTEND_URL`,
Google keys, `BOOTSTRAP_ADMIN_EMAILS`). Finally import employees once: run `scripts\import_employees.py` locally with
`DATABASE_URL` set to the database's *external* URL. Always run local backend commands from `backend/` (relative `./storage/...`
paths resolve against the current folder; running from the repo root creates a stray `storage/`).

### Permanent QR code
```powershell
python scripts\generate_qr.py --url https://attendance.gwcdata.ai --out storage\qr\entrance.png
```
Contains only the URL (no query/employee data); same for everyone; `FRONTEND_URL` changes the default. Print the â‰ˆ2000 px PNG at
â‰¥15Ã—15 cm on matte stock at ~1.2â€“1.5 m near the entrance, test-scan with Android and iPhone. No screen or laptop needed.

## Security summary
HTTP-only SameSite signed session cookie (Secure in prod) Â· session-bound CSRF + Origin check on writes Â· CSP/HSTS/nosniff Â·
rate limits (punch, auth, assistant) Â· Pydantic validation, parameterised SQL Â· DB CHECK/UNIQUE/FK + indexes Â· append-only
events Â· no passwords stored Â· secrets only via environment (Domo credentials never reach the browser) Â· audit log for punches,
rejections, logins, admin actions, HR-sheet imports, agent runs/tool calls, downloads Â· no biometric data.
Limits: signed client-side session (role/status are re-read from the DB each request); in-memory rate limiting; IP check â‰  presence.
Enforce 2-Step Verification for admin accounts in Google Workspace (not enforceable from this app).

## Backup & recovery
PostgreSQL is the only state that matters; the workbook is reproducible (`python scripts\generate_excel.py`).
Nightly `pg_dump -Fc -h HOST -U USER attendance > attendance_YYYY-MM-DD.dump` (keep 30 daily + 12 monthly, copy off-host,
encrypt; dumps contain personal data). Restore: empty DB â†’ `pg_restore -d attendance --clean --if-exists FILE` â†’
`alembic upgrade head` â†’ regenerate workbook. Test a restore quarterly. Keep secrets separately; rotating `SECRET_KEY` logs everyone out.


## Folder layout
- `backend/` - everything Python: `app/`, `scripts/`, `tests/`, `storage/`, `alembic.ini`, `requirements.txt`, `.env`, `start_server.ps1`. Run all backend commands from this folder.
- `frontend/` - everything web: `src/`, `package.json`, `vercel.json`, `.env`, `start_frontend.ps1`. Deploy this folder to Vercel (Root Directory = `frontend`).
- The two only talk over HTTP: `VITE_API_BASE_URL` (frontend) and `FRONTEND_URL` (backend; also the single CORS origin).

