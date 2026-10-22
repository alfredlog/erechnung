"""Writes an Invoice as UN/CEFACT CII XML (D16B), the syntax used by XRechnung-CII and ZUGFeRD 2.x.

Element order follows the CII schema exactly: the schema is a strict sequence,
so a correct element in the wrong place makes the whole file invalid.
Works for any Invoice: one built by the form, or one read from a PDF by the agent.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from app.domain.models import Invoice, Party

RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
NSMAP = {"rsm": RSM, "ram": RAM, "qdt": QDT, "udt": UDT}
PEPPOL_PROCESS = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


class InvoiceIncomplete(ValueError):
    pass


def _el(parent, tag: str, text: str | None = None, **attrs) -> etree._Element:
    prefix, name = tag.split(":")
    el = etree.SubElement(parent, f"{{{NSMAP[prefix]}}}{name}", **attrs)
    if text is not None:
        el.text = text
    return el


def _amount(v: Decimal | None) -> str:
    return f"{(v or Decimal(0)):.2f}"


def _num(v: Decimal) -> str:
    """Quantities, prices and rates without needless zeros: 19, 1.5, 0.125"""
    s = format(v.normalize(), "f")
    return s if s not in ("-0", "") else "0"


def _date(parent, tag: str, d: date) -> None:
    holder = _el(parent, tag)
    _el(holder, "udt:DateTimeString", d.strftime("%Y%m%d"), format="102")


def _party(parent, tag: str, p: Party, *, seller: bool) -> None:
    party = _el(parent, tag)
    _el(party, "ram:Name", p.name)
    if seller and (p.contact_name or p.phone or p.email):
        contact = _el(party, "ram:DefinedTradeContact")
        if p.contact_name:
            _el(contact, "ram:PersonName", p.contact_name)
        if p.phone:
            _el(_el(contact, "ram:TelephoneUniversalCommunication"), "ram:CompleteNumber", p.phone)
        if p.email:
            _el(_el(contact, "ram:EmailURIUniversalCommunication"), "ram:URIID", p.email)
    a = p.address
    addr = _el(party, "ram:PostalTradeAddress")
    if a.postal_code:
        _el(addr, "ram:PostcodeCode", a.postal_code)
    if a.street:
        _el(addr, "ram:LineOne", a.street)
    if a.city:
        _el(addr, "ram:CityName", a.city)
    _el(addr, "ram:CountryID", a.country_code or "DE")
    if p.email:  # electronic address BT-34 / BT-49, scheme EM = e-mail
        _el(_el(party, "ram:URIUniversalCommunication"), "ram:URIID", p.email, schemeID="EM")
    if p.vat_id:
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", p.vat_id, schemeID="VA")
    if p.tax_number and seller:
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", p.tax_number, schemeID="FC")


def write_cii(inv: Invoice) -> bytes:
    if not (inv.number and inv.issue_date and inv.currency and inv.seller.name and inv.buyer.name and inv.lines):
        raise InvoiceIncomplete("Number, date, currency, seller, buyer and at least one line are required.")
    t = inv.totals

    root = etree.Element(f"{{{RSM}}}CrossIndustryInvoice", nsmap=NSMAP)
    ctx = _el(root, "rsm:ExchangedDocumentContext")
    if inv.specification and "xrechnung" in inv.specification:
        _el(_el(ctx, "ram:BusinessProcessSpecifiedDocumentContextParameter"), "ram:ID", PEPPOL_PROCESS)
    _el(_el(ctx, "ram:GuidelineSpecifiedDocumentContextParameter"), "ram:ID",
        inv.specification or "urn:cen.eu:en16931:2017")

    doc = _el(root, "rsm:ExchangedDocument")
    _el(doc, "ram:ID", inv.number)
    _el(doc, "ram:TypeCode", inv.type_code or "380")
    _date(doc, "ram:IssueDateTime", inv.issue_date)
    if inv.note:
        _el(_el(doc, "ram:IncludedNote"), "ram:Content", inv.note)

    tx = _el(root, "rsm:SupplyChainTradeTransaction")
    for i, ln in enumerate(inv.lines, start=1):
        item = _el(tx, "ram:IncludedSupplyChainTradeLineItem")
        _el(_el(item, "ram:AssociatedDocumentLineDocument"), "ram:LineID", ln.line_id or str(i))
        _el(_el(item, "ram:SpecifiedTradeProduct"), "ram:Name", ln.name or "Position")
        price = _el(_el(item, "ram:SpecifiedLineTradeAgreement"), "ram:NetPriceProductTradePrice")
        _el(price, "ram:ChargeAmount", _num(ln.unit_price if ln.unit_price is not None else ln.net_amount))
        _el(_el(item, "ram:SpecifiedLineTradeDelivery"), "ram:BilledQuantity",
            _num(ln.quantity if ln.quantity is not None else Decimal(1)), unitCode=ln.unit_code or "C62")
        settle = _el(item, "ram:SpecifiedLineTradeSettlement")
        tax = _el(settle, "ram:ApplicableTradeTax")
        _el(tax, "ram:TypeCode", "VAT")
        _el(tax, "ram:CategoryCode", ln.vat_category or "S")
        _el(tax, "ram:RateApplicablePercent", _num(ln.vat_rate if ln.vat_rate is not None else Decimal(0)))
        _el(_el(settle, "ram:SpecifiedTradeSettlementLineMonetarySummation"), "ram:LineTotalAmount",
            _amount(ln.net_amount))

    agreement = _el(tx, "ram:ApplicableHeaderTradeAgreement")
    if inv.buyer_reference:
        _el(agreement, "ram:BuyerReference", inv.buyer_reference)
    _party(agreement, "ram:SellerTradeParty", inv.seller, seller=True)
    _party(agreement, "ram:BuyerTradeParty", inv.buyer, seller=False)

    delivery = _el(tx, "ram:ApplicableHeaderTradeDelivery")
    if inv.delivery_date:
        _date(_el(delivery, "ram:ActualDeliverySupplyChainEvent"), "ram:OccurrenceDateTime", inv.delivery_date)

    settlement = _el(tx, "ram:ApplicableHeaderTradeSettlement")
    _el(settlement, "ram:PaymentReference", inv.number)        # BT-83: Verwendungszweck
    _el(settlement, "ram:InvoiceCurrencyCode", inv.currency)
    if inv.payment.iban:
        means = _el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        _el(means, "ram:TypeCode", "58")                       # 58 = SEPA credit transfer
        _el(_el(means, "ram:PayeePartyCreditorFinancialAccount"), "ram:IBANID", inv.payment.iban)
        if inv.payment.bic:
            _el(_el(means, "ram:PayeeSpecifiedCreditorFinancialInstitution"), "ram:BICID", inv.payment.bic)
    else:
        _el(_el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans"), "ram:TypeCode", "1")  # not defined

    for b in inv.vat_breakdown:
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:CalculatedAmount", _amount(b.tax_amount))
        _el(tax, "ram:TypeCode", "VAT")
        if b.exemption_reason:
            _el(tax, "ram:ExemptionReason", b.exemption_reason)
        _el(tax, "ram:BasisAmount", _amount(b.taxable_amount))
        _el(tax, "ram:CategoryCode", b.category or "S")
        _el(tax, "ram:RateApplicablePercent", _num(b.rate if b.rate is not None else Decimal(0)))

    if inv.payment.terms or inv.due_date:
        terms = _el(settlement, "ram:SpecifiedTradePaymentTerms")
        if inv.payment.terms:
            _el(terms, "ram:Description", inv.payment.terms)
        if inv.due_date:
            _date(terms, "ram:DueDateDateTime", inv.due_date)

    summ = _el(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _el(summ, "ram:LineTotalAmount", _amount(t.line_total))
    if t.charges:
        _el(summ, "ram:ChargeTotalAmount", _amount(t.charges))
    if t.allowances:
        _el(summ, "ram:AllowanceTotalAmount", _amount(t.allowances))
    _el(summ, "ram:TaxBasisTotalAmount", _amount(t.net_total))
    _el(summ, "ram:TaxTotalAmount", _amount(t.vat_total), currencyID=inv.currency)
    if t.rounding:
        _el(summ, "ram:RoundingAmount", _amount(t.rounding))
    _el(summ, "ram:GrandTotalAmount", _amount(t.gross_total))
    if t.prepaid:
        _el(summ, "ram:TotalPrepaidAmount", _amount(t.prepaid))
    _el(summ, "ram:DuePayableAmount", _amount(t.amount_due))

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
