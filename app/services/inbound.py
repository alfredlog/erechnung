"""Receiving addresses: every company gets <name>-<random>@<INBOUND_DOMAIN>.

The SMTP server (app.mailin) asks `lookup` whether a recipient exists and hands
accepted mails to `deliver`. Invoice attachments go to the agent exactly like
uploads; mails without attachments (e.g. Gmail's forwarding confirmation) are
kept as notices so the customer can read them.
"""
from __future__ import annotations

import email
import re
import secrets
import unicodedata
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as default_policy
from html import unescape

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.tables import InboundAddress, InboundNotice, Organization
from app.services import agent, audit
from app.services.mail import attachments

KEEP_NOTICES = 20
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read and type
_LINK = re.compile(r"https://[^\s<>\"')\]]+")


def _slug(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.replace("ß", "ss")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:24].strip("-") or "firma"


def address_for(db: Session, org: Organization) -> InboundAddress:
    """The company's receiving address, created on first use."""
    row = db.scalar(select(InboundAddress).where(InboundAddress.organization_id == org.id))
    if row:
        return row
    while True:
        local = f"{_slug(org.name)}-{''.join(secrets.choice(ALPHABET) for _ in range(6))}"
        if not db.scalar(select(InboundAddress).where(InboundAddress.local_part == local)):
            break
    row = InboundAddress(organization_id=org.id, local_part=local)
    db.add(row)
    audit.log(db, org.id, "inbound_address_created", address=local)
    db.commit()
    return row


def full_address(row: InboundAddress) -> str:
    return f"{row.local_part}@{settings.inbound_domain}"


def lookup(db: Session, address: str) -> int | None:
    """Organization id for a recipient address, or None. Ignores case and +tags."""
    local, _, domain = address.strip().strip("<>").lower().rpartition("@")
    if not local or not settings.inbound_domain or domain != settings.inbound_domain:
        return None
    local = local.split("+", 1)[0]
    return db.scalar(select(InboundAddress.organization_id).where(InboundAddress.local_part == local))


def _header(msg: Message, name: str) -> str:
    value = msg.get(name, "")
    try:
        return str(make_header(decode_header(str(value))))
    except (UnicodeError, LookupError, ValueError):
        return str(value)


def _text(msg: Message) -> str:
    plain, html = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    if plain is not None:
        return plain
    if html is not None:
        hrefs = " ".join(re.findall(r'href="(https://[^"]+)"', html))
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
        return re.sub(r"[ \t]+", " ", text).strip() + ("\n" + hrefs if hrefs else "")
    return ""


def deliver(db: Session, org_id: int, raw: bytes, sender: str = "") -> int:
    """Takes one received mail for a company. Returns the number of new invoices."""
    org = db.get(Organization, org_id)
    if org is None:
        return 0
    new = 0
    for name, content in attachments(raw):
        _inv, is_new = agent.receive(db, org, name, content, source="email")
        new += is_new
    msg = email.message_from_bytes(raw, policy=default_policy)
    subject = _header(msg, "Subject")[:500]
    sender = (_header(msg, "From") or sender)[:320]
    if new:
        audit.log(db, org.id, "mail_received", sender=sender, subject=subject, new_documents=new)
    elif not attachments(raw):
        body = _text(msg)[:20000]
        links = list(dict.fromkeys(_LINK.findall(body)))[:10]
        db.add(InboundNotice(organization_id=org.id, sender=sender, subject=subject, body=body, links=links))
        db.flush()
        keep = select(InboundNotice.id).where(InboundNotice.organization_id == org.id).order_by(
            InboundNotice.received_at.desc(), InboundNotice.id.desc()).limit(KEEP_NOTICES)
        db.execute(delete(InboundNotice).where(InboundNotice.organization_id == org.id,
                                               InboundNotice.id.not_in(keep)))
        audit.log(db, org.id, "mail_without_invoice", sender=sender, subject=subject)
    db.commit()
    return new


def notices(db: Session, org_id: int, limit: int = 5) -> list[InboundNotice]:
    return list(db.scalars(select(InboundNotice).where(InboundNotice.organization_id == org_id)
                           .order_by(InboundNotice.received_at.desc(), InboundNotice.id.desc()).limit(limit)))
