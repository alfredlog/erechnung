"""E-mail notifications (only when sending is configured).

- Daily at 8:00 (German time): "N invoices are waiting for you" to Admins and employees who
  did not switch it off, only if something is waiting.
- To Admins: the free trial ends in 3 days / has ended.
Each notification is remembered (sent_notifications), so nobody gets one twice.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.tables import Invoice, NotificationSetting, Organization, SentNotification, Subscription, User
from app.services import outbox

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Berlin")
DIGEST_HOUR = 8


def wants_digest(db: Session, user: User) -> bool:
    if user.role not in ("owner", "member"):
        return False
    pref = db.get(NotificationSetting, user.id)
    return pref.daily_digest if pref else True


def _already(db: Session, user_id: int, kind: str, ref: str) -> bool:
    return db.scalar(select(SentNotification.id).where(SentNotification.user_id == user_id,
                                                       SentNotification.kind == kind,
                                                       SentNotification.ref == ref)) is not None


def _send(db: Session, user: User, kind: str, ref: str, subject: str, body: str) -> bool:
    if _already(db, user.id, kind, ref):
        return False
    db.add(SentNotification(user_id=user.id, kind=kind, ref=ref))
    try:
        db.flush()  # claims it first: a second worker cannot send the same mail
    except IntegrityError:
        db.rollback()
        return False
    try:
        outbox.send_system(user.email, subject, body)
    except Exception:  # noqa: BLE001  try again next time
        db.rollback()
        log.exception("notification %s to user %s failed", kind, user.id)
        return False
    db.commit()
    return True


def _eur(value) -> str:
    return "" if value is None else f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _digest_body(org: Organization, rows: list[Invoice], waiting: int, held: int) -> str:
    base = settings.base_url.rstrip("/")
    lines = ["Hallo,", ""]
    if waiting:
        lines.append(f"in {settings.app_name} warten {waiting} Rechnung(en) von „{org.name}“ auf deine Prüfung:")
        lines.append("")
        for r in rows:
            reason = (r.review_reasons or [""])[0]
            lines.append(f"• {r.partner_name or 'Unbekannter Lieferant'} – {r.number or 'ohne Nummer'} – "
                         f"{_eur(r.amount_due)}" + (f"\n  {reason}" if reason else ""))
        if waiting > len(rows):
            lines.append(f"• … und {waiting - len(rows)} weitere")
        lines.append("")
    if held:
        lines += [f"{held} Rechnung(en) warten, weil das Monatslimit deines Tarifs erreicht ist. "
                  f"Upgraden: {base}/tarif", ""]
    lines += [f"Zum Eingang: {base}/eingang?f=offen", "",
              f"Diese E-Mail kommt höchstens einmal am Tag und nur, wenn etwas wartet. Abschalten: {base}/konto",
              "", f"Viele Grüße\n{settings.app_name}"]
    return "\n".join(lines)


def daily_digests(db: Session, today: str) -> int:
    counts = db.execute(select(Invoice.organization_id, Invoice.status, func.count()).where(
        Invoice.direction == "in", Invoice.status.in_(("needs_review", "limit")))
        .group_by(Invoice.organization_id, Invoice.status)).all()
    by_org: dict[int, dict[str, int]] = {}
    for org_id, status, n in counts:
        by_org.setdefault(org_id, {})[status] = n
    sent = 0
    for org_id, c in by_org.items():
        org = db.get(Organization, org_id)
        rows = list(db.scalars(select(Invoice).where(Invoice.organization_id == org_id,
                                                     Invoice.status == "needs_review", Invoice.direction == "in")
                               .order_by(Invoice.id).limit(5)))
        waiting, held = c.get("needs_review", 0), c.get("limit", 0)
        subject = (f"{waiting} Rechnung(en) warten auf deine Prüfung" if waiting
                   else f"{held} Rechnung(en) warten wegen des Monatslimits")
        body = _digest_body(org, rows, waiting, held)
        for user in db.scalars(select(User).where(User.organization_id == org_id, User.role.in_(("owner", "member")))):
            if wants_digest(db, user):
                sent += _send(db, user, "digest", today, f"{subject} – {settings.app_name}", body)
    return sent


def trial_reminders(db: Session, now: datetime) -> int:
    from app.services import billing
    base = settings.base_url.rstrip("/")
    sent = 0
    subs = db.scalars(select(Subscription).where(Subscription.trial_ends_at.is_not(None),
                                                 Subscription.trial_ends_at > now - timedelta(days=2),
                                                 Subscription.trial_ends_at < now + timedelta(days=3)))
    for sub in subs:
        if billing.has_paid_plan(sub):
            continue
        end = billing._aware(sub.trial_ends_at)
        ended = end <= now
        kind = "trial_ended" if ended else "trial_ending"
        ref = end.date().isoformat()
        if ended:
            subject = f"Dein Test von {settings.app_name} ist beendet"
            text = ("dein kostenloser Test ist beendet. Dein Konto läuft im Tarif Free weiter (5 Eingangsrechnungen "
                    "im Monat) – es geht nichts verloren.")
        else:
            subject = f"Dein Test von {settings.app_name} endet am {end.astimezone(TZ):%d.%m.%Y}"
            text = (f"dein kostenloser Test endet am {end.astimezone(TZ):%d.%m.%Y}. Danach läuft dein Konto im "
                    "Tarif Free weiter, wenn du keinen Tarif wählst.")
        body = (f"Hallo,\n\n{text}\n\nTarif wählen: {base}/tarif\n\nViele Grüße\n{settings.app_name}")
        for owner in db.scalars(select(User).where(User.organization_id == sub.organization_id,
                                                   User.role == "owner")):
            sent += _send(db, owner, kind, ref, subject, body)
    return sent


def run_due(db: Session, now: datetime | None = None) -> int:
    """Called by the worker about once a minute."""
    if not outbox.enabled():
        return 0
    now = now or datetime.now(UTC)
    local = now.astimezone(TZ)
    sent = 0
    if local.hour >= DIGEST_HOUR:
        sent += daily_digests(db, local.date().isoformat())
    if settings.billing_enabled and local.hour >= DIGEST_HOUR:
        sent += trial_reminders(db, now)
    return sent
