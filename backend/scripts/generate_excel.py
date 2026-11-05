"""Regenerate the attendance workbook from the database (storage/reports/attendance.xlsx)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database.connection import SessionLocal, init_db  # noqa: E402
from app.services.excel_generator import generate_report  # noqa: E402

if __name__ == "__main__":
    if not get_settings().is_production:
        init_db()
    with SessionLocal() as db:
        print("Generated:", generate_report(db))
