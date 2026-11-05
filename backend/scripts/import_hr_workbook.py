"""Apply HR's edits to the 'Shift Master' and 'Employee Master' sheets of an Excel workbook to the database.

  python scripts/import_hr_workbook.py path\\to\\edited.xlsx

Only those two sheets are read; the attendance sheets are generated output and are ignored. The whole file is
validated first: if anything is invalid NOTHING is changed and every problem is listed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database.connection import SessionLocal, init_db  # noqa: E402
from app.services import hr_import  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    settings = get_settings()
    if not settings.is_production:
        init_db()
    with SessionLocal() as db:
        try:
            res = hr_import.import_workbook(db, settings, sys.argv[1], actor="cli-hr-import")
        except hr_import.HRImportError as exc:
            db.rollback()
            print("REJECTED - nothing was changed:")
            for e in exc.errors:
                print("  -", e)
            return 1
    print("Imported:", {k: v for k, v in res.items() if k != "warnings"})
    for w in res["warnings"]:
        print("  warning:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
