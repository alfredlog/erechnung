from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password, hash_token, new_token, verify_password
from app.core.tables import Organization, SessionToken, User
from app.services import audit

SESSION_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(ValueError):
    pass


def register(db: Session, company: str, email: str, password: str) -> User:
    email = email.strip().lower()
    if not company.strip():
        raise AccountError("Bitte den Firmennamen angeben.")
    if not EMAIL_RE.match(email):
        raise AccountError("Bitte eine gültige E-Mail-Adresse angeben.")
    if len(password) < 10:
        raise AccountError("Das Passwort muss mindestens 10 Zeichen lang sein.")
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise AccountError("Für diese E-Mail-Adresse gibt es schon ein Konto.")
    org = Organization(name=company.strip(), profile={"name": company.strip(), "email": email})
    db.add(org)
    db.flush()
    user = User(organization_id=org.id, email=email, password_hash=hash_password(password), role="owner")
    db.add(user)
    db.flush()
    audit.log(db, org.id, "account_created", user_id=user.id)
    db.commit()
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user and verify_password(password, user.password_hash):
        return user
    return None


def start_session(db: Session, user: User) -> str:
    token = new_token()
    db.add(SessionToken(token_hash=hash_token(token), user_id=user.id,
                        expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS)))
    db.execute(delete(SessionToken).where(SessionToken.expires_at < datetime.now(UTC)))
    db.commit()
    return token


def user_for_token(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    s = db.get(SessionToken, hash_token(token))
    if s is None:
        return None
    expires = s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        return None
    return s.user


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(SessionToken).where(SessionToken.token_hash == hash_token(token)))
        db.commit()
