"""DEVELOPMENT ONLY demo data. Refuses to run when APP_ENV=production.

Creates SAMPLE shifts and fictional employees (unregistered, so you can test registration through DEV_AUTH).
The sample shift timings are NOT company policy - in a real deployment HR defines shifts in the workbook's
Shift Master sheet. Never run against a real database.
"""
import sys
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.connection import SessionLocal, init_db  # noqa: E402
from app.database.models import ShiftMaster  # noqa: E402
from app.services.employee_service import upsert_master_employee  # noqa: E402

if __name__ == "__main__":
    s = get_settings()
    if s.is_production:
        sys.exit("Refusing to seed demo data in production.")
    init_db()
    d = s.allowed_email_domain
    with SessionLocal() as db:
        if not db.scalar(select(ShiftMaster).where(ShiftMaster.shift_name == "DEMO General")):
            db.add(ShiftMaster(shift_name="DEMO General", start_time=time(9, 0), end_time=time(18, 0),
                               late_tolerance_minutes=10, early_tolerance_minutes=10, active=True))
            db.add(ShiftMaster(shift_name="DEMO Night", start_time=time(22, 0), end_time=time(6, 0),
                               late_tolerance_minutes=10, early_tolerance_minutes=10, active=True))
        for i, name in enumerate(["Demo Asha", "Demo Ravi", "Demo Meena"], start=1):
            upsert_master_employee(db, s, employee_id=f"DEMO{i:03d}", name=name, email=f"demo.user{i}@{d}",
                                   department="Demo", assigned_shift="DEMO Night" if i == 3 else "DEMO General")
        db.commit()
    print("Demo shifts + employees created (DEV ONLY, sample timings are not policy).")
