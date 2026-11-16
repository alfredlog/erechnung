"""SMTP handler: accepts mail only for existing receiving addresses, never relays or sends.

Protection:
- unknown recipients are refused in RCPT (550), so the server is useless for spammers
- at most MAX_RCPT recipients per mail and MAX_PER_WINDOW mails per sending IP per window
- size limit (MAX_MAIL_MB) enforced by aiosmtpd before the data is read completely
- on an internal error we answer 451: the sender's server keeps the mail and retries later
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import ssl
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP

from app.core.config import settings
from app.core.db import SessionLocal
from app.services import inbound, jobs

log = logging.getLogger("mailin")
MAX_RCPT = 5
MAX_PER_WINDOW = 30
WINDOW_SECONDS = 600


class InboundHandler:
    def __init__(self) -> None:
        self._seen: dict[str, deque] = defaultdict(deque)

    def _too_many(self, ip: str) -> bool:
        now = time.monotonic()
        q = self._seen[ip]
        while q and now - q[0] > WINDOW_SECONDS:
            q.popleft()
        return len(q) >= MAX_PER_WINDOW

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802 (aiosmtpd API)
        ip = (session.peer or ("?",))[0]
        if self._too_many(ip):
            return "421 4.7.0 Zu viele E-Mails, bitte später erneut versuchen"
        if len(envelope.rcpt_tos) >= MAX_RCPT:
            return "452 4.5.3 Zu viele Empfänger"
        org_id = await asyncio.get_running_loop().run_in_executor(None, _lookup, address)
        if org_id is None:
            log.info("refused recipient %s from %s", address, ip)
            return "550 5.1.1 Empfängeradresse unbekannt"
        envelope.rcpt_tos.append(address)
        envelope.org_ids = sorted({*getattr(envelope, "org_ids", []), org_id})
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 (aiosmtpd API)
        ip = (session.peer or ("?",))[0]
        self._seen[ip].append(time.monotonic())
        raw = envelope.original_content or envelope.content
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        try:
            loop = asyncio.get_running_loop()
            for org_id in getattr(envelope, "org_ids", []):
                new = await loop.run_in_executor(None, _deliver, org_id, raw, envelope.mail_from or "")
                log.info("mail for org %s from %s: %s new invoice(s)", org_id, ip, new)
        except Exception:  # noqa: BLE001  never lose a mail: let the sender retry
            log.exception("could not store mail from %s", ip)
            return "451 4.3.0 Vorübergehender Fehler, bitte später erneut versuchen"
        return "250 OK Rechnung empfangen"


def _lookup(address: str) -> int | None:
    with SessionLocal() as db:
        return inbound.lookup(db, address)


def _deliver(org_id: int, raw: bytes, sender: str) -> int:
    with SessionLocal() as db:
        new = inbound.deliver(db, org_id, raw, sender)
        jobs.run_pending_inline(db)
        return new


def tls_context(hostname: str, folder: Path | None = None) -> ssl.SSLContext:
    """Self-signed certificate for STARTTLS. Sending mail servers use TLS opportunistically and
    do not check the certificate, so this still encrypts invoices on the way."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder = folder or Path(tempfile.mkdtemp(prefix="mailin-tls-"))
    folder.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    if not (cert_file.exists() and key_file.exists()):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        now = dt.datetime.now(dt.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .sign(key, hashes.SHA256()))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        key_file.chmod(0o600)
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


class _Controller(Controller):
    def __init__(self, handler, tls: ssl.SSLContext | None, **kwargs):
        self._tls = tls
        super().__init__(handler, **kwargs)

    def factory(self):
        return SMTP(self.handler, hostname=settings.inbound_domain or "localhost",
                    data_size_limit=settings.max_mail_mb * 1024 * 1024, tls_context=self._tls,
                    require_starttls=False, decode_data=False, ident="E-Rechnung Agent")


def make_controller(host: str = "0.0.0.0", port: int | None = None, tls: ssl.SSLContext | None = None):
    return _Controller(InboundHandler(), tls, hostname=host, port=port or settings.smtp_port)
