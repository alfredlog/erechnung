from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.tables import AuditLog


def log(db: Session, org_id: int, action: str, *, user_id: int | None = None, invoice_id: int | None = None,
        **detail) -> None:
    db.add(AuditLog(organization_id=org_id, user_id=user_id, invoice_id=invoice_id, action=action,
                    detail={k: v for k, v in detail.items() if v is not None}))
