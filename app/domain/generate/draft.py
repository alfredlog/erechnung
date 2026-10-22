"""What a user types into the "create invoice" form, and how we turn it into a complete invoice.

The user only enters facts (who, what, how many, what price, which VAT rate).
Everything that can be calculated is calculated here, so totals are always
consistent: line amounts, VAT per rate, net/gross totals, amount due.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.domain.models import (
    Address,
    Invoice,
    InvoiceLine,
    Party,
    Payment,
    SourceFormat,
    Totals,
    VatBreakdown,
)

CENT = Decimal("0.01")
KLEINUNTERNEHMER_REASON = "Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG"


class Profile(StrEnum):
    XRECHNUNG = "xrechnung"   # German CIUS, required by public authorities
    EN16931 = "en16931"       # plain EU standard, used inside ZUGFeRD / Factur-X PDFs


SPECIFICATION = {
    Profile.XRECHNUNG: "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0",
    Profile.EN16931: "urn:cen.eu:en16931:2017",
}


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class DraftParty(BaseModel):
    name: str = ""
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country_code: str = "DE"
    vat_id: str = ""
    tax_number: str = ""
    email: str = ""
    contact_name: str = ""
    phone: str = ""

    def to_party(self) -> Party:
        def n(v: str) -> str | None:
            return v.strip() or None
        return Party(
            name=n(self.name), vat_id=n(self.vat_id), tax_number=n(self.tax_number), email=n(self.email),
            contact_name=n(self.contact_name), phone=n(self.phone),
            address=Address(street=n(self.street), postal_code=n(self.postal_code), city=n(self.city),
                            country_code=(self.country_code.strip().upper() or "DE")),
        )


class DraftLine(BaseModel):
    name: str
    quantity: Decimal = Decimal(1)
    unit_code: str = "C62"          # C62 = piece; HUR = hour; DAY = day; LS = lump sum
    unit_price: Decimal             # net, per unit
    vat_rate: Decimal = Decimal(19)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Bezeichnung fehlt")
        return v.strip()


class InvoiceDraft(BaseModel):
    profile: Profile = Profile.EN16931
    type_code: str = "380"          # 380 invoice, 381 credit note
    number: str
    issue_date: date = Field(default_factory=date.today)
    delivery_date: date | None = None
    due_days: int = 14
    currency: str = "EUR"
    buyer_reference: str = ""       # Leitweg-ID for authorities, otherwise e.g. order number
    seller: DraftParty
    buyer: DraftParty
    lines: list[DraftLine]
    iban: str = ""
    bic: str = ""
    payment_terms: str = ""
    note: str = ""
    small_business: bool = False    # Kleinunternehmer § 19 UStG: no VAT

    @field_validator("lines")
    @classmethod
    def _lines(cls, v: list[DraftLine]) -> list[DraftLine]:
        if not v:
            raise ValueError("Mindestens eine Position angeben")
        return v

    # ------------------------------------------------------------------ checks
    def missing_fields(self) -> list[str]:
        """German list of what is still missing. Empty list = ready to generate.

        Mirrors the hard rules of EN 16931 and XRechnung (BR-DE-*), so the user gets
        a clear message instead of a validator error code.
        """
        s, b = self.seller, self.buyer
        missing = []
        req = [(self.number, "Rechnungsnummer"), (s.name, "Name des Rechnungsstellers"),
               (s.street, "Straße des Rechnungsstellers"), (s.postal_code, "PLZ des Rechnungsstellers"),
               (s.city, "Ort des Rechnungsstellers"), (b.name, "Name des Empfängers"),
               (b.street, "Straße des Empfängers"), (b.postal_code, "PLZ des Empfängers"),
               (b.city, "Ort des Empfängers")]
        missing += [label for value, label in req if not str(value).strip()]
        if not (s.vat_id.strip() or s.tax_number.strip()):
            missing.append("USt-IdNr. oder Steuernummer des Rechnungsstellers")
        if self.profile == Profile.XRECHNUNG:
            req = [(s.contact_name, "Ansprechpartner (Rechnungssteller)"),
                   (s.phone, "Telefon (Rechnungssteller)"), (s.email, "E-Mail (Rechnungssteller)"),
                   (b.email, "E-Mail des Empfängers"), (self.iban, "IBAN")]
            missing += [label for value, label in req if not str(value).strip()]
        return missing

    # ------------------------------------------------------------- calculation
    def to_invoice(self) -> Invoice:
        lines: list[InvoiceLine] = []
        groups: OrderedDict[tuple[str, Decimal], Decimal] = OrderedDict()
        for i, dl in enumerate(self.lines, start=1):
            category, rate = ("E", Decimal(0)) if self.small_business else (
                ("S", dl.vat_rate) if dl.vat_rate > 0 else ("Z", Decimal(0)))
            net = money(dl.quantity * dl.unit_price)
            lines.append(InvoiceLine(line_id=str(i), name=dl.name, quantity=dl.quantity, unit_code=dl.unit_code,
                                     unit_price=dl.unit_price, net_amount=net, vat_category=category,
                                     vat_rate=rate))
            groups[(category, rate)] = groups.get((category, rate), Decimal(0)) + net

        breakdown = [
            VatBreakdown(category=cat, rate=rate, taxable_amount=money(base), tax_amount=money(base * rate / 100),
                         exemption_reason=KLEINUNTERNEHMER_REASON if cat == "E" else None)
            for (cat, rate), base in groups.items()
        ]
        line_total = money(sum((ln.net_amount for ln in lines), Decimal(0)))
        vat_total = money(sum((b.tax_amount for b in breakdown), Decimal(0)))
        gross = line_total + vat_total

        terms = self.payment_terms.strip() or (
            f"Zahlbar innerhalb von {self.due_days} Tagen ohne Abzug." if self.due_days else "Zahlbar sofort.")
        note = self.note.strip() or None
        if self.small_business and KLEINUNTERNEHMER_REASON not in (note or ""):
            note = f"{note}\n{KLEINUNTERNEHMER_REASON}" if note else KLEINUNTERNEHMER_REASON

        return Invoice(
            source_format=SourceFormat.CREATED,
            specification=SPECIFICATION[self.profile],
            number=self.number.strip(),
            type_code=self.type_code,
            issue_date=self.issue_date,
            due_date=self.issue_date + timedelta(days=self.due_days),
            delivery_date=self.delivery_date or self.issue_date,
            currency=self.currency,
            buyer_reference=self.buyer_reference.strip() or "n/a",
            note=note,
            seller=self.seller.to_party(),
            buyer=self.buyer.to_party(),
            lines=lines,
            vat_breakdown=breakdown,
            totals=Totals(line_total=line_total, net_total=line_total, vat_total=vat_total, gross_total=gross,
                          amount_due=gross),
            payment=Payment(iban=self.iban.replace(" ", "").upper() or None,
                            bic=self.bic.replace(" ", "").upper() or None, terms=terms),
        )
