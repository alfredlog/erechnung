"""Parser for UN/CEFACT CII: XRechnung CII, ZUGFeRD 2.x and Factur-X XML."""
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
from app.domain.parse._xml import Finder, to_date

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
f = Finder(NS)
LINE_TOTAL = (
    "ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount"
)


def _party(p) -> Party:
    if p is None:
        return Party()
    addr = f.node(p, "ram:PostalTradeAddress")
    return Party(
        name=f.text(p, "ram:Name"),
        vat_id=f.text(p, "ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']"),
        tax_number=f.text(p, "ram:SpecifiedTaxRegistration/ram:ID[@schemeID='FC']"),
        address=Address(
            street=f.text(addr, "ram:LineOne"),
            city=f.text(addr, "ram:CityName"),
            postal_code=f.text(addr, "ram:PostcodeCode"),
            country_code=f.text(addr, "ram:CountryID"),
        ),
        email=f.text(p, "ram:URIUniversalCommunication/ram:URIID")
        or f.text(p, "ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID"),
    )


def parse_cii(root: etree._Element, source: SourceFormat = SourceFormat.CII) -> Invoice:
    doc = f.node(root, "rsm:ExchangedDocument")
    tx = f.node(root, "rsm:SupplyChainTradeTransaction")
    agreement = f.node(tx, "ram:ApplicableHeaderTradeAgreement")
    settlement = f.node(tx, "ram:ApplicableHeaderTradeSettlement")
    currency = f.text(settlement, "ram:InvoiceCurrencyCode")

    lines = []
    for ln in f.all(tx, "ram:IncludedSupplyChainTradeLineItem"):
        qty = f.node(ln, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity")
        tax = f.node(ln, "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax")
        lines.append(InvoiceLine(
            line_id=f.text(ln, "ram:AssociatedDocumentLineDocument/ram:LineID"),
            name=f.text(ln, "ram:SpecifiedTradeProduct/ram:Name"),
            quantity=f.dec(ln, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity"),
            unit_code=qty.get("unitCode") if qty is not None else None,
            unit_price=f.dec(ln, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount"),
            net_amount=f.dec(ln, LINE_TOTAL),
            vat_category=f.text(tax, "ram:CategoryCode"),
            vat_rate=f.dec(tax, "ram:RateApplicablePercent"),
        ))

    breakdown = [
        VatBreakdown(
            category=f.text(t, "ram:CategoryCode"),
            rate=f.dec(t, "ram:RateApplicablePercent"),
            taxable_amount=f.dec(t, "ram:BasisAmount"),
            tax_amount=f.dec(t, "ram:CalculatedAmount"),
        )
        for t in f.all(settlement, "ram:ApplicableTradeTax")
    ]

    s = f.node(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    pm = f.node(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
    terms = f.node(settlement, "ram:SpecifiedTradePaymentTerms")
    vat_total = f.dec(s, f"ram:TaxTotalAmount[@currencyID='{currency}']") or f.dec(s, "ram:TaxTotalAmount")

    return Invoice(
        source_format=source,
        specification=f.text(
            root, "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID"
        ),
        number=f.text(doc, "ram:ID"),
        type_code=f.text(doc, "ram:TypeCode"),
        issue_date=to_date(f.text(doc, "ram:IssueDateTime/udt:DateTimeString")),
        due_date=to_date(f.text(terms, "ram:DueDateDateTime/udt:DateTimeString")),
        currency=currency,
        buyer_reference=f.text(agreement, "ram:BuyerReference"),
        seller=_party(f.node(agreement, "ram:SellerTradeParty")),
        buyer=_party(f.node(agreement, "ram:BuyerTradeParty")),
        lines=lines,
        vat_breakdown=breakdown,
        totals=Totals(
            line_total=f.dec(s, "ram:LineTotalAmount"),
            allowances=f.dec(s, "ram:AllowanceTotalAmount"),
            charges=f.dec(s, "ram:ChargeTotalAmount"),
            net_total=f.dec(s, "ram:TaxBasisTotalAmount"),
            vat_total=vat_total,
            gross_total=f.dec(s, "ram:GrandTotalAmount"),
            prepaid=f.dec(s, "ram:TotalPrepaidAmount"),
            rounding=f.dec(s, "ram:RoundingAmount"),
            amount_due=f.dec(s, "ram:DuePayableAmount"),
        ),
        payment=Payment(
            iban=f.text(pm, "ram:PayeePartyCreditorFinancialAccount/ram:IBANID"),
            bic=f.text(pm, "ram:PayeeSpecifiedCreditorFinancialInstitution/ram:BICID"),
            terms=f.text(terms, "ram:Description"),
        ),
    )
