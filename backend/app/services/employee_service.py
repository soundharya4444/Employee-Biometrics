from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import AdminUser, Employee
from app.services.audit import audit

EMP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class RegistrationError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def email_in_domain(email: str, settings: Settings) -> bool:
    e = normalise_email(email)
    return bool(EMAIL_RE.match(e)) and e.endswith("@" + settings.allowed_email_domain) \
        and e.count("@") == 1


def employee_by_identity(db: Session, sub: str | None, email: str) -> Employee | None:
    """Look up by the bound Google subject first (stable), else by verified e-mail for not-yet-bound records."""
    if sub:
        emp = db.scalar(select(Employee).where(Employee.google_sub == sub))
        if emp:
            return emp
    emp = db.scalar(select(Employee).where(Employee.verified_email == normalise_email(email)))
    if emp and emp.google_sub and sub and emp.google_sub != sub:
        return None
    return emp if emp and emp.registered_at else None


def register_employee(db: Session, settings: Settings, *, sub: str | None, email: str, employee_id: str,
                      employee_name: str) -> Employee:
    email = normalise_email(email)
    if not email_in_domain(email, settings):
        raise RegistrationError("domain", f"Only @{settings.allowed_email_domain} accounts are allowed", 403)
    employee_id = (employee_id or "").strip().upper()
    employee_name = " ".join((employee_name or "").split())
    if not EMP_ID_RE.match(employee_id):
        raise RegistrationError("employee_id", "Employee ID may contain letters, digits, - and _ (2-32 chars)", 422)
    if employee_by_identity(db, sub, email):
        raise RegistrationError("already_registered", "This account is already registered", 409)

    master = db.scalar(select(Employee).where(func.upper(Employee.employee_id) == employee_id))
    now = datetime.now(UTC)
    if master is not None:
        if master.registered_at is not None:
            audit(db, email, "REGISTER_REJECTED", "employee", employee_id, {"why": "id already claimed"})
            db.commit()
            raise RegistrationError("duplicate", "This Employee ID is already registered", 409)
        if master.verified_email != email:
            audit(db, email, "REGISTER_REJECTED", "employee", employee_id, {"why": "email mismatch with master list"})
            db.commit()
            raise RegistrationError("mismatch", "Employee ID does not match your company account. Contact HR.", 403)
        master.google_sub, master.registered_at = sub, now  # name stays as per HR master list
        emp = master
    else:
        if settings.require_master_list:
            audit(db, email, "REGISTER_REJECTED", "employee", employee_id, {"why": "not in master list"})
            db.commit()
            raise RegistrationError("not_authorised", "Employee ID not found in the authorised employee list. Contact HR.", 403)
        if not (2 <= len(employee_name) <= 200):
            raise RegistrationError("employee_name", "Employee name is required", 422)
        if db.scalar(select(Employee).where(Employee.verified_email == email)):
            raise RegistrationError("duplicate", "This email is already registered", 409)
        emp = Employee(employee_id=employee_id, employee_name=employee_name, verified_email=email,
                       account_status="PENDING_APPROVAL", google_sub=sub, registered_at=now)
        db.add(emp)
    audit(db, email, "EMPLOYEE_REGISTERED", "employee", employee_id, {"status": emp.account_status})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RegistrationError("duplicate", "Employee ID or account is already registered", 409)
    return emp


def employee_public(emp: Employee) -> dict:
    return {"employee_id": emp.employee_id, "employee_name": emp.employee_name, "mail_id": emp.verified_email,
            "department": emp.department, "account_status": emp.account_status}


def upsert_master_employee(db: Session, settings: Settings, *, employee_id: str, name: str, email: str,
                           department: str | None = None, designation: str | None = None,
                           location: str | None = None, assigned_shift: str | None = None,
                           actor: str = "import") -> str:
    email = normalise_email(email)
    employee_id = employee_id.strip().upper()
    if not email_in_domain(email, settings):
        raise ValueError(f"{email}: not an @{settings.allowed_email_domain} address")
    if not EMP_ID_RE.match(employee_id):
        raise ValueError(f"{employee_id}: invalid employee id")
    if not name or not name.strip():
        raise ValueError(f"{employee_id}: employee name is required")
    emp = db.scalar(select(Employee).where(Employee.employee_id == employee_id))
    if emp is None:
        if db.scalar(select(Employee).where(Employee.verified_email == email)):
            raise ValueError(f"{email}: already assigned to another employee")
        db.add(Employee(employee_id=employee_id, employee_name=name.strip(), verified_email=email,
                        department=department, designation=designation, location=location,
                        assigned_shift=(assigned_shift or "").strip() or None))
        audit(db, actor, "EMPLOYEE_IMPORTED", "employee", employee_id, {"mode": "create"})
        return "created"
    if emp.registered_at and emp.verified_email != email:
        raise ValueError(f"{employee_id}: verified e-mail cannot be changed by import (admin process required)")
    emp.employee_name, emp.verified_email = name.strip(), email
    emp.department, emp.designation, emp.location = department, designation, location
    emp.assigned_shift = (assigned_shift or "").strip() or None
    audit(db, actor, "EMPLOYEE_IMPORTED", "employee", employee_id, {"mode": "update"})
    return "updated"


# ---------- admins ----------
def seed_bootstrap_admins(db: Session, settings: Settings) -> None:
    for email in settings.bootstrap_admins:
        if not email_in_domain(email, settings):
            continue
        if not db.scalar(select(AdminUser).where(AdminUser.email == email)):
            db.add(AdminUser(email=email, role="SUPER_ADMIN", status="ACTIVE"))
            audit(db, "system", "ADMIN_SEEDED", "admin", email, {})
    db.commit()


def get_admin(db: Session, email: str) -> AdminUser | None:
    return db.scalar(select(AdminUser).where(AdminUser.email == normalise_email(email), AdminUser.status == "ACTIVE"))
