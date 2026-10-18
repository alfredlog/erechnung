"""Format-independent invoice model, based on the EN 16931 semantic model.

Every parser (UBL, CII, LLM extraction) produces this same object, so the rest
of the system (checks, review, DATEV export) never cares where an invoice came from.
BT-xx comments refer to the business terms of EN 16931.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class SourceFormat(StrEnum):
    UBL = "ubl"            # XRechnung, UBL syntax
    CII = "cii"            # XRechnung CII / ZUGFeRD / Factur-X XML
    ZUGFERD_PDF = "zugferd_pdf"  # PDF/A-3 with embedded CII XML
    PDF = "pdf"            # plain PDF, needs extraction
    IMAGE = "image"        # scan / photo, needs OCR
    UNKNOWN = "unknown"


class Address(BaseModel):
    street: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country_code: str | None = None  # BT-40 / BT-55


class Party(BaseModel):
    name: str | None = None          # BT-27 / BT-44
    vat_id: str | None = None        # BT-31 / BT-48 (USt-IdNr.)
    tax_number: str | None = None    # BT-32 (Steuernummer)
    address: Address = Field(default_factory=Address)
    email: str | None = None


class InvoiceLine(BaseModel):
    line_id: str | None = None       # BT-126
    name: str | None = None          # BT-153
    quantity: Decimal | None = None  # BT-129
    unit_code: str | None = None     # BT-130
    unit_price: Decimal | None = None  # BT-146
    net_amount: Decimal | None = None  # BT-131
    vat_category: str | None = None  # BT-151
    vat_rate: Decimal | None = None  # BT-152


class VatBreakdown(BaseModel):
    category: str | None = None      # BT-118
    rate: Decimal | None = None      # BT-119
    taxable_amount: Decimal | None = None  # BT-116
    tax_amount: Decimal | None = None      # BT-117


class Totals(BaseModel):
    line_total: Decimal | None = None       # BT-106  sum of line net amounts
    allowances: Decimal | None = None       # BT-107
    charges: Decimal | None = None          # BT-108
    net_total: Decimal | None = None        # BT-109  total without VAT
    vat_total: Decimal | None = None        # BT-110
    gross_total: Decimal | None = None      # BT-112  total with VAT
    prepaid: Decimal | None = None          # BT-113
    rounding: Decimal | None = None         # BT-114
    third_party: Decimal | None = None      # BT-DEX-002 sum (XRechnung extension, third-party payments)
    amount_due: Decimal | None = None       # BT-115


class Payment(BaseModel):
    iban: str | None = None          # BT-84
    bic: str | None = None           # BT-86
    terms: str | None = None         # BT-20


class Invoice(BaseModel):
    source_format: SourceFormat
    specification: str | None = None  # BT-24, e.g. ...xrechnung_3.0
    number: str | None = None         # BT-1
    type_code: str | None = None      # BT-3 (380 invoice, 381 credit note)
    issue_date: date | None = None    # BT-2
    due_date: date | None = None      # BT-9
    currency: str | None = None       # BT-5
    buyer_reference: str | None = None  # BT-10 (Leitweg-ID for public sector)
    seller: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    lines: list[InvoiceLine] = Field(default_factory=list)
    vat_breakdown: list[VatBreakdown] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)
    payment: Payment = Field(default_factory=Payment)
