"""Shift lookup and schedule maths. All shift data comes from the shift_master table (HR-maintained)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Employee, ShiftMaster


@dataclass(frozen=True)
class ShiftInfo:
    name: str
    start: time
    end: time
    late_tol: int
    early_tol: int

    @property
    def overnight(self) -> bool:
        return self.end <= self.start


def load_shifts(db: Session, include_inactive: bool = False) -> dict[str, ShiftInfo]:
    q = select(ShiftMaster)
    if not include_inactive:
        q = q.where(ShiftMaster.active.is_(True))
    return {s.shift_name.strip().lower(): ShiftInfo(s.shift_name, s.start_time, s.end_time,
                                                    s.late_tolerance_minutes, s.early_tolerance_minutes)
            for s in db.scalars(q)}


def resolve_shift(emp: Employee, shifts: dict[str, ShiftInfo]) -> tuple[ShiftInfo | None, str | None]:
    """Returns (shift, issue). Never invents a schedule: no/unknown/inactive mapping -> (None, issue)."""
    name = (emp.assigned_shift or "").strip()
    if not name:
        return None, "NO_SHIFT_ASSIGNED"
    s = shifts.get(name.lower())
    if s is None:
        return None, f"UNKNOWN_OR_INACTIVE_SHIFT:{name}"
    return s, None


def scheduled_start(shift: ShiftInfo, d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, shift.start, tzinfo=tz)


def scheduled_end(shift: ShiftInfo, d: date, tz: ZoneInfo) -> datetime:
    end = datetime.combine(d, shift.end, tzinfo=tz)
    return end + timedelta(days=1) if shift.overnight else end


def late_delay_minutes(shift: ShiftInfo, in_ts: datetime, work_date: date, tz: ZoneInfo) -> int:
    """Whole minutes after the scheduled start (negative = early arrival)."""
    return int((in_ts - scheduled_start(shift, work_date, tz)).total_seconds() // 60)


def early_departure_minutes(shift: ShiftInfo, out_ts: datetime, work_date: date, tz: ZoneInfo) -> int:
    """Whole minutes before the scheduled end (negative = stayed late)."""
    return int((scheduled_end(shift, work_date, tz) - out_ts).total_seconds() // 60)


def fmt_minutes(m: int) -> str:
    h, mm = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if mm or not h:
        parts.append(f"{mm} minute{'s' if mm != 1 else ''}")
    return " ".join(parts)


def fmt_clock(t: datetime | time) -> str:
    return t.strftime("%I:%M %p")
