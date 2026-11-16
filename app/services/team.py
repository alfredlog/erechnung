"""Several people per company: invitations, roles, removal.

Roles: owner (Admin: everything), member (Mitarbeiter: daily work, no settings/plan/team),
advisor (Steuerberater: read and export only). Admins and employees count against the plan's
user limit; tax advisors do not (up to MAX_ADVISORS).

Removed users are not deleted (the audit log must keep saying who approved what). Their
login is disabled and the e-mail address is freed for a new account.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.tables import Invitation, Organization, SessionToken, User
from app.services import audit
from app.services.accounts import EMAIL_RE, AccountError, hash_password, hash_token, new_token

INVITE_DAYS = 7
MAX_ADVISORS = 3
COUNTED = ("owner", "member")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def members(db: Session, org_id: int) -> list[User]:
    return list(db.scalars(select(User).where(User.organization_id == org_id, User.role != "removed")
                           .order_by(User.id)))


def pending(db: Session, org_id: int) -> list[Invitation]:
    rows = db.scalars(select(Invitation).where(Invitation.organization_id == org_id,
                                               Invitation.accepted_at.is_(None)).order_by(Invitation.created_at))
    return [r for r in rows if _aware(r.expires_at) > datetime.now(UTC)]


def seats(db: Session, org: Organization) -> dict:
    from app.services import billing
    plan = billing.effective_plan(db, org)
    people = members(db, org.id)
    invites = pending(db, org.id)
    used = sum(u.role in COUNTED for u in people) + sum(i.role == "member" for i in invites)
    advisors = sum(u.role == "advisor" for u in people) + sum(i.role == "advisor" for i in invites)
    return {"limit": plan.users, "used": used, "advisors": advisors, "plan": plan}


def invite(db: Session, org: Organization, by: User, email: str, role: str) -> tuple[Invitation, str]:
    email = email.strip().lower()
    if role not in ("member", "advisor"):
        raise AccountError("Unbekannte Rolle.")
    if not EMAIL_RE.match(email):
        raise AccountError("Bitte eine gültige E-Mail-Adresse angeben.")
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise AccountError("Diese E-Mail-Adresse hat bereits ein Konto bei uns. Bitte eine andere Adresse nutzen.")
    if any(i.email == email for i in pending(db, org.id)):
        raise AccountError("Diese Person ist bereits eingeladen.")
    s = seats(db, org)
    if role == "member" and s["used"] >= s["limit"]:
        raise AccountError(f"Dein Tarif {s['plan'].name} enthält {s['limit']} Nutzer. Unter „Tarif“ kannst du "
                           "upgraden.")
    if role == "advisor" and s["advisors"] >= MAX_ADVISORS:
        raise AccountError(f"Es sind höchstens {MAX_ADVISORS} Steuerberater-Zugänge möglich.")
    token = new_token()
    row = Invitation(token_hash=hash_token(token), organization_id=org.id, email=email, role=role,
                     invited_by=by.id, expires_at=datetime.now(UTC) + timedelta(days=INVITE_DAYS))
    db.add(row)
    audit.log(db, org.id, "user_invited", user_id=by.id, email=email, role=role)
    db.commit()
    return row, token


def valid_invitation(db: Session, token: str) -> Invitation | None:
    if not token:
        return None
    row = db.get(Invitation, hash_token(token))
    if row is None or row.accepted_at is not None or _aware(row.expires_at) < datetime.now(UTC):
        return None
    return row


def accept(db: Session, token: str, password: str) -> User:
    row = valid_invitation(db, token)
    if row is None:
        raise AccountError("Diese Einladung ist ungültig oder abgelaufen. Bitte lass dich erneut einladen.")
    if len(password) < 10:
        raise AccountError("Das Passwort muss mindestens 10 Zeichen lang sein.")
    if db.scalar(select(User).where(func.lower(User.email) == row.email)):
        raise AccountError("Für diese E-Mail-Adresse gibt es schon ein Konto.")
    user = User(organization_id=row.organization_id, email=row.email, password_hash=hash_password(password),
                role=row.role)
    db.add(user)
    row.accepted_at = datetime.now(UTC)
    db.flush()
    audit.log(db, row.organization_id, "user_joined", user_id=user.id, email=row.email, role=row.role)
    db.commit()
    return user


def cancel(db: Session, org: Organization, by: User, token_hash: str) -> None:
    row = db.get(Invitation, token_hash)
    if row is None or row.organization_id != org.id or row.accepted_at is not None:
        raise AccountError("Einladung nicht gefunden.")
    db.delete(row)
    audit.log(db, org.id, "invitation_cancelled", user_id=by.id, email=row.email)
    db.commit()


def change_role(db: Session, org: Organization, by: User, user_id: int, role: str) -> None:
    user = db.get(User, user_id)
    if user is None or user.organization_id != org.id or user.role == "removed":
        raise AccountError("Nutzer nicht gefunden.")
    if role not in ("owner", "member", "advisor"):
        raise AccountError("Unbekannte Rolle.")
    if user.id == by.id:
        raise AccountError("Die eigene Rolle kannst du nicht ändern.")
    if role in COUNTED and user.role not in COUNTED:
        s = seats(db, org)
        if s["used"] >= s["limit"]:
            raise AccountError(f"Dein Tarif enthält {s['limit']} Nutzer.")
    old, user.role = user.role, role
    audit.log(db, org.id, "role_changed", user_id=by.id, target=user.id, old=old, new=role)
    db.commit()


def remove(db: Session, org: Organization, by: User, user_id: int) -> None:
    user = db.get(User, user_id)
    if user is None or user.organization_id != org.id or user.role == "removed":
        raise AccountError("Nutzer nicht gefunden.")
    if user.id == by.id:
        raise AccountError("Du kannst dich nicht selbst entfernen.")
    email = user.email
    user.role = "removed"
    user.email = f"entfernt-{user.id}-{new_token()[:8]}@invalid.local"  # frees the address
    user.password_hash = hash_password(new_token())                     # nobody knows it: login impossible
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    audit.log(db, org.id, "user_removed", user_id=by.id, target=user.id, email=email)
    db.commit()
