"""Background worker: `python -m app.worker` (its own Docker container).

Loop: take the next job, run it; every few minutes, queue a mailbox check for
each company that connected one. Sleeps briefly when there is nothing to do.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.core.tables import Job, Organization
from app.services import jobs

log = logging.getLogger("worker")
_running = True


def _stop(*_):
    global _running
    _running = False


def schedule_mailboxes(db) -> None:
    due = datetime.now(UTC) - timedelta(minutes=settings.imap_poll_minutes)
    for org in db.scalars(select(Organization)):
        if not (org.imap or {}).get("host"):
            continue
        last = org.imap_last_poll
        if last is not None and (last if last.tzinfo else last.replace(tzinfo=UTC)) > due:
            continue
        pending = db.scalar(select(Job).where(Job.organization_id == org.id, Job.kind == "poll_mailbox",
                                              Job.status.in_(("queued", "running"))))
        if not pending:
            jobs.enqueue(db, org.id, "poll_mailbox", {})
    db.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    init_db()
    log.info("worker started (LLM: %s, KoSIT: %s)", "on" if settings.llm_enabled else "off",
             settings.kosit_url or "off")
    last_schedule = 0.0
    while _running:
        with SessionLocal() as db:
            if time.monotonic() - last_schedule > 60:
                schedule_mailboxes(db)
                from app.services import billing
                billing.release_all(db)  # invoices held by the monthly limit, once there is room
                from app.services import notify
                notify.run_due(db)       # daily digest at 8:00, trial reminders
                beat = settings.data_dir / ".heartbeat-worker"  # the monitor checks the worker is alive
                beat.parent.mkdir(parents=True, exist_ok=True)
                beat.write_text(str(int(time.time())))
                last_schedule = time.monotonic()
            job = jobs.claim(db)
            if job:
                log.info("job %s %s", job.id, job.kind)
                jobs.run(db, job)
                continue
        time.sleep(2)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
