"""Parser for XRechnung in UBL syntax (Invoice and CreditNote)."""
from __future__ import annotations

from lxml import etree

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
from app.domain.parse._xml import Finder, to_date, to_decimal

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
f = Finder(NS)


def _party(p) -> Party:
    if p is None:
        return Party()
    addr = f.node(p, "cac:PostalAddress")
    return Party(
        name=f.text(p, "cac:PartyLegalEntity/cbc:RegistrationName") or f.text(p, "cac:PartyName/cbc:Name"),
        vat_id=f.text(p, "cac:PartyTaxScheme[cac:TaxScheme/cbc:ID='VAT']/cbc:CompanyID"),
        tax_number=f.text(p, "cac:PartyTaxScheme[cac:TaxScheme/cbc:ID!='VAT']/cbc:CompanyID"),
        address=Address(
            street=f.text(addr, "cbc:StreetName"),
            city=f.text(addr, "cbc:CityName"),
            postal_code=f.text(addr, "cbc:PostalZone"),
            country_code=f.text(addr, "cac:Country/cbc:IdentificationCode"),
        ),
        email=f.text(p, "cac:Contact/cbc:ElectronicMail"),
    )


def _third_party(root):
    """XRechnung extension BG-DEX-09: amounts collected on behalf of third parties."""
    amounts = [to_decimal(x) for x in f.all(root, "cac:PrepaidPayment/cbc:PaidAmount/text()")]
    amounts = [a for a in amounts if a is not None]
    return sum(amounts) if amounts else None


def parse_ubl(root: etree._Element) -> Invoice:
    is_credit = etree.QName(root).localname == "CreditNote"
    line_tag = "cac:CreditNoteLine" if is_credit else "cac:InvoiceLine"
    qty_tag = "cbc:CreditedQuantity" if is_credit else "cbc:InvoicedQuantity"
    currency = f.text(root, "cbc:DocumentCurrencyCode")

    lines = []
    for ln in f.all(root, line_tag):
        qty = f.node(ln, qty_tag)
        lines.append(InvoiceLine(
            line_id=f.text(ln, "cbc:ID"),
            name=f.text(ln, "cac:Item/cbc:Name"),
            quantity=f.dec(ln, qty_tag),
            unit_code=qty.get("unitCode") if qty is not None else None,
            unit_price=f.dec(ln, "cac:Price/cbc:PriceAmount"),
            net_amount=f.dec(ln, "cbc:LineExtensionAmount"),
            vat_category=f.text(ln, "cac:Item/cac:ClassifiedTaxCategory/cbc:ID"),
            vat_rate=f.dec(ln, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent"),
        ))

    # Only the TaxTotal in the document currency carries the VAT breakdown (BG-23).
    tax_total = f.node(root, f"cac:TaxTotal[cbc:TaxAmount/@currencyID='{currency}']")
    if tax_total is None:
        tax_total = f.node(root, "cac:TaxTotal")
    breakdown = [
        VatBreakdown(
            category=f.text(st, "cac:TaxCategory/cbc:ID"),
            rate=f.dec(st, "cac:TaxCategory/cbc:Percent"),
            taxable_amount=f.dec(st, "cbc:TaxableAmount"),
            tax_amount=f.dec(st, "cbc:TaxAmount"),
        )
        for st in f.all(tax_total, "cac:TaxSubtotal")
    ]

    m = f.node(root, "cac:LegalMonetaryTotal")
    pm = f.node(root, "cac:PaymentMeans")
    return Invoice(
        source_format=SourceFormat.UBL,
        specification=f.text(root, "cbc:CustomizationID"),
        number=f.text(root, "cbc:ID"),
        type_code=f.text(root, "cbc:CreditNoteTypeCode" if is_credit else "cbc:InvoiceTypeCode"),
        issue_date=to_date(f.text(root, "cbc:IssueDate")),
        due_date=to_date(f.text(root, "cbc:DueDate") or f.text(root, "cac:PaymentMeans/cbc:PaymentDueDate")),
        currency=currency,
        buyer_reference=f.text(root, "cbc:BuyerReference"),
        seller=_party(f.node(root, "cac:AccountingSupplierParty/cac:Party")),
        buyer=_party(f.node(root, "cac:AccountingCustomerParty/cac:Party")),
        lines=lines,
        vat_breakdown=breakdown,
        totals=Totals(
            line_total=f.dec(m, "cbc:LineExtensionAmount"),
            allowances=f.dec(m, "cbc:AllowanceTotalAmount"),
            charges=f.dec(m, "cbc:ChargeTotalAmount"),
            net_total=f.dec(m, "cbc:TaxExclusiveAmount"),
            vat_total=f.dec(tax_total, "cbc:TaxAmount"),
            gross_total=f.dec(m, "cbc:TaxInclusiveAmount"),
            prepaid=f.dec(m, "cbc:PrepaidAmount"),
            rounding=f.dec(m, "cbc:PayableRoundingAmount"),
            third_party=_third_party(root),
            amount_due=f.dec(m, "cbc:PayableAmount"),
        ),
        payment=Payment(
            iban=f.text(pm, "cac:PayeeFinancialAccount/cbc:ID"),
            bic=f.text(pm, "cac:PayeeFinancialAccount/cac:FinancialInstitutionBranch/cbc:ID"),
            terms=f.text(root, "cac:PaymentTerms/cbc:Note"),
        ),
    )
