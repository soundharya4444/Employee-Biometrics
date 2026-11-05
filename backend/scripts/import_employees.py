"""Import the authorised employee master list from CSV (HR-owned data).

CSV header: employee_id,employee_name,email[,department,designation,location,shift]
  shift must match a Shift Master name (maintained by HR in the workbook). Unmapped employees are flagged, never guessed.

  python scripts/import_employees.py employees.csv
  python scripts/import_employees.py employees.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database.connection import SessionLocal, init_db  # noqa: E402
from app.services.employee_service import upsert_master_employee  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("csv_file")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    settings = get_settings()
    if not settings.is_production:
        init_db()
    ok = bad = 0
    with SessionLocal() as db, open(a.csv_file, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            try:
                res = upsert_master_employee(
                    db, settings, employee_id=r.get("employee_id", ""), name=r.get("employee_name", ""),
                    email=r.get("email", ""), department=r.get("department") or None,
                    designation=r.get("designation") or None, location=r.get("location") or None,
                    assigned_shift=r.get("shift") or None)
                ok += 1
                print(f"line {n}: {r.get('employee_id')} {res}")
            except ValueError as exc:
                bad += 1
                print(f"line {n}: SKIPPED - {exc}", file=sys.stderr)
        if a.dry_run:
            db.rollback()
            print("dry run: nothing saved")
        else:
            db.commit()
    print(f"done: {ok} ok, {bad} skipped")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

