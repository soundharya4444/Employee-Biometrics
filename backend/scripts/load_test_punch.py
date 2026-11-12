"""Load test: N employees sign in and punch IN at the same moment, each double-submitting the same request.

Run it ONLY against a throw-away backend (it writes attendance rows):
  1) start a scratch server with its own database, DEV_AUTH_ENABLED=true and PUNCH_IN_OPENS_AT= (empty)
  2) python scripts/load_test_punch.py --base http://127.0.0.1:8765 --employees 30
Employees E000.. with e-mails emp0@<domain>.. must already be in the master list (see --seed in this file's docstring
of scratch_server.py usage, or import them with scripts/import_employees.py).
Checks: every request succeeds, exactly one IN per employee (no duplicates, none lost), and reports latency.
"""
from __future__ import annotations

import argparse
import statistics
import threading
import time

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--employees", type=int, default=30)
    ap.add_argument("--domain", default="gwcdata.ai")
    a = ap.parse_args()

    clients, csrfs = [], []
    for i in range(a.employees):  # sign-in + registration (not part of the timed burst)
        c = httpx.Client(base_url=a.base, headers={**UA, "Origin": a.base}, timeout=30)
        c.post("/api/auth/dev-login", json={"email": f"emp{i}@{a.domain}"}).raise_for_status()
        csrf = c.get("/api/me").json()["csrf"]
        r = c.post("/api/register", json={"employee_id": f"E{i:03d}", "employee_name": f"Emp {i}"}, headers={"X-CSRF-Token": csrf})
        r.raise_for_status()
        clients.append(c); csrfs.append(csrf)

    barrier = threading.Barrier(a.employees * 2)
    codes, lat, lock = [], [], threading.Lock()

    def go(i):
        barrier.wait()
        t0 = time.perf_counter()
        r = clients[i].post("/api/attendance/punch", json={"type": "IN", "request_key": f"load-{i}"},
                            headers={"X-CSRF-Token": csrfs[i]})
        with lock:
            codes.append(r.status_code); lat.append((time.perf_counter() - t0) * 1000)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(a.employees) for _ in range(2)]
    t0 = time.perf_counter()
    [t.start() for t in ts]; [t.join() for t in ts]
    wall = (time.perf_counter() - t0) * 1000

    ins = sum(1 for i in range(a.employees)
              if clients[i].get("/api/attendance/today").json()["is_punched_in"])
    ok = codes.count(200) == len(codes) == a.employees * 2 and ins == a.employees
    print(f"requests={len(codes)} ok={codes.count(200)} punched_in_employees={ins}/{a.employees}")
    print(f"latency ms: median={statistics.median(lat):.0f} p95={sorted(lat)[int(len(lat) * .95) - 1]:.0f} max={max(lat):.0f} | wall={wall:.0f}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
