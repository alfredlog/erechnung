"""Background jobs stored in PostgreSQL.

The web request only stores the file and adds a row here, so uploads answer
instantly. The worker process takes rows one by one. `FOR UPDATE SKIP LOCKED`
lets several workers run without ever taking the same job twice.
"""
from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.tables import Job

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def enqueue(db: Session, org_id: int, kind: str, payload: dict) -> Job:
    job = Job(organization_id=org_id, kind=kind, payload=payload)
    db.add(job)
    db.flush()
    return job


def claim(db: Session) -> Job | None:
    stmt = (select(Job).where(Job.status == "queued", Job.run_after <= datetime.now(UTC))
            .order_by(Job.id).limit(1))
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = db.scalar(stmt)
    if job:
        job.status = "running"
        job.attempts += 1
        db.commit()
    return job


def run(db: Session, job: Job) -> None:
    from app.core.tables import Invoice
    from app.services import agent, mail

    try:
        if job.kind == "process_invoice":
            row = db.get(Invoice, job.payload["invoice_id"])
            if row is not None and row.status == "processing":
                agent.process(db, row, force_invoice=bool(job.payload.get("force_invoice")))
        elif job.kind == "poll_mailbox":
            mail.poll(db, job.organization_id)
        elif job.kind == "send_invoice_mail":
            from app.services import outbox
            outbox.send(db, job.payload["mail_id"])
        else:
            raise ValueError(f"unknown job kind {job.kind}")
        job.status = "done"
        job.last_error = None
    except Exception as exc:
        db.rollback()
        log.exception("job %s failed", job.id)
        job.last_error = "".join(traceback.format_exception_only(exc))[:2000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            if job.kind == "process_invoice":
                row = db.get(Invoice, job.payload.get("invoice_id"))
                if row is not None:
                    row.status = "needs_review"
                    row.review_reasons = ["Automatische Verarbeitung fehlgeschlagen – bitte manuell prüfen."]
            elif job.kind == "send_invoice_mail":
                from app.core.tables import OutgoingMail
                from app.services import outbox
                m = db.get(OutgoingMail, job.payload.get("mail_id"))
                if m is not None and m.status != "sent":
                    outbox.mark_failed(db, m, m.error or "Versand nach mehreren Versuchen fehlgeschlagen.")
        else:
            job.status = "queued"
            job.run_after = datetime.now(UTC) + timedelta(minutes=2 ** job.attempts)  # back off
    db.commit()


def run_pending_inline(db: Session) -> int:
    """Development/tests: process the queue right away instead of in the worker."""
    if not settings.inline_jobs:
        return 0
    done = 0
    while (job := claim(db)) is not None:
        run(db, job)
        done += 1
    return done
