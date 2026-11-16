"""Database tables.

Multi-tenant: every business row carries organization_id, and every query in the
web layer filters on the logged-in user's organization.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def now() -> datetime:
    return datetime.now(UTC)


Money = Numeric(14, 2)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Company data used when creating invoices (DraftParty fields) + bank + numbering
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    iban: Mapped[str] = mapped_column(String(40), default="")
    bic: Mapped[str] = mapped_column(String(20), default="")
    small_business: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice_prefix: Mapped[str] = mapped_column(String(20), default="RE-")
    next_invoice_number: Mapped[int] = mapped_column(Integer, default=1)
    # Agent policy: auto-approve invoices that pass every check
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=True)
    # DATEV export settings (Beraternummer, Mandantennummer, accounts)
    datev: Mapped[dict] = mapped_column(JSON, default=dict)
    # IMAP mailbox the agent reads (password stored encrypted)
    imap: Mapped[dict] = mapped_column(JSON, default=dict)
    imap_last_poll: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_creditor_account: Mapped[int] = mapped_column(Integer, default=70000)
    next_debitor_account: Mapped[int] = mapped_column(Integer, default=10000)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner / member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    organization: Mapped[Organization] = relationship()


class SessionToken(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship()


class Supplier(Base):
    """A business partner we received invoices from (or sent invoices to)."""
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("organization_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200))  # VAT id if known, else normalised name
    name: Mapped[str] = mapped_column(String(300))
    vat_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    iban: Mapped[str | None] = mapped_column(String(40), nullable=True)  # IBAN confirmed by a human
    datev_account: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Kreditor/Debitor number
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    """An original file as received. Never modified (GoBD: unchangeable archive)."""
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("organization_id", "sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))  # upload / email / created
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(3), default="in")  # in = received, out = created by us
    # processing -> needs_review / approved -> exported ; or rejected / failed
    status: Mapped[str] = mapped_column(String(20), default="processing", index=True)
    source_format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    number: Mapped[str | None] = mapped_column(String(200), nullable=True)
    issue_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    partner_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    net_total: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    vat_total: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    gross_total: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    amount_due: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    iban: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)       # full domain Invoice model
    findings: Mapped[list] = mapped_column(JSON, default=list)          # rule + KoSIT results
    confidence: Mapped[dict] = mapped_column(JSON, default=dict)        # per-field confidence (extraction)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)    # why a human must look
    kosit_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    export_id: Mapped[int | None] = mapped_column(ForeignKey("exports.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    document: Mapped[Document | None] = relationship()
    supplier: Mapped[Supplier | None] = relationship()


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # datev / csv
    filename: Mapped[str] = mapped_column(String(200))
    invoice_count: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    """Work queue in the database (no Redis needed): the worker claims rows with SKIP LOCKED."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="queued", index=True)  # queued/running/done/failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    """Append-only history: who did what, when (GoBD traceability). Never updated or deleted."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True,
                                                   index=True)
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InboundAddress(Base):
    """The company's own receiving address: <local_part>@<INBOUND_DOMAIN>.

    Customers forward invoices there (or give it to suppliers), so no mailbox password is needed.
    """
    __tablename__ = "inbound_addresses"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), unique=True)
    local_part: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InboundNotice(Base):
    """A mail to the receiving address without an invoice attachment, e.g. the confirmation mail
    Gmail or Outlook send when a forwarding is set up. Shown to the customer, last ones only."""
    __tablename__ = "inbound_notices"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sender: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    links: Mapped[list] = mapped_column(JSON, default=list)
