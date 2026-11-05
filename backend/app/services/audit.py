from __future__ import annotations

from sqlalchemy.orm import Session

from app.database.models import AuditLog


def audit(db: Session, actor: str, action: str, entity_type: str | None = None,
          entity_id: str | int | None = None, meta: dict | None = None) -> AuditLog:
    """Add an audit row to the current transaction (caller commits)."""
    row = AuditLog(actor=actor, action=action, entity_type=entity_type,
                   entity_id=None if entity_id is None else str(entity_id), meta=meta or {})
    db.add(row)
    return row
