"""Reads invoices from an e-mail inbox (IMAP).

Each customer can connect a mailbox (e.g. rechnungen@firma.de). The worker checks
it regularly, takes every PDF/XML attachment of unread mails into the agent, and
marks the mail as read.
"""
from __future__ import annotations

import email
import imaplib
import logging
from datetime import UTC, datetime
from email.header import decode_header, make_header

from sqlalchemy.orm import Session

from app.core.security import decrypt
from app.core.tables import Organization
from app.services import agent, audit

log = logging.getLogger(__name__)
ACCEPTED = (".pdf", ".xml", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
MAX_MAILS_PER_RUN = 50


def _filename(part) -> str | None:
    name = part.get_filename()
    if not name:
        return None
    try:
        return str(make_header(decode_header(name)))
    except (UnicodeError, LookupError):
        return name


IMAGES = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
MIN_IMAGE_BYTES = 30_000  # smaller pictures are logos/icons in signatures, not scanned invoices


def _is_signature_image(part, name: str | None, data: bytes) -> bool:
    is_image = part.get_content_maintype() == "image" or (name or "").lower().endswith(IMAGES)
    if not is_image:
        return False
    inline = (part.get("Content-Disposition") or "").lower().startswith("inline") or part.get("Content-ID")
    return bool(inline) or len(data) < MIN_IMAGE_BYTES


def attachments(raw: bytes) -> list[tuple[str, bytes]]:
    msg = email.message_from_bytes(raw)
    out = []
    for part in msg.walk():
        name = _filename(part)
        ctype = part.get_content_type()
        if name and name.lower().endswith(ACCEPTED) or ctype in ("application/pdf", "application/xml", "text/xml"):
            data = part.get_payload(decode=True)
            if data and not _is_signature_image(part, name, data):
                out.append((name or f"anhang.{ 'pdf' if 'pdf' in ctype else 'xml'}", data))
    return out


def connect(cfg: dict) -> imaplib.IMAP4:
    host, port = cfg["host"], int(cfg.get("port") or 993)
    conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    conn.login(cfg["user"], decrypt(cfg.get("password", "")))
    return conn


def poll(db: Session, org_id: int, conn_factory=connect) -> int:
    org = db.get(Organization, org_id)
    cfg = org.imap or {}
    if not cfg.get("host"):
        return 0
    taken = 0
    conn = conn_factory(cfg)
    try:
        conn.select(cfg.get("folder") or "INBOX")
        _typ, data = conn.search(None, "UNSEEN")
        for num in (data[0].split() if data and data[0] else [])[:MAX_MAILS_PER_RUN]:
            _typ, msg_data = conn.fetch(num, "(RFC822)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            for name, content in attachments(raw):
                _inv, new = agent.receive(db, org, name, content, source="email")
                taken += new
            conn.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001  connection already gone
            pass
    org.imap_last_poll = datetime.now(UTC)
    if taken:
        audit.log(db, org.id, "mailbox_polled", new_documents=taken)
    db.commit()
    return taken
