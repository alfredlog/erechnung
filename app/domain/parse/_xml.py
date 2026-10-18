"""Small XML helpers shared by the UBL and CII parsers."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from lxml import etree

# Hardened parser: no external entities, no network access (XXE protection).
SAFE_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True, remove_comments=True
)


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, parser=SAFE_PARSER)


class Finder:
    """Wraps an element and a namespace map so XPath calls stay short."""

    def __init__(self, ns: dict[str, str]):
        self.ns = ns

    def all(self, el, path: str) -> list:
        return el.xpath(path, namespaces=self.ns) if el is not None else []

    def node(self, el, path: str):
        found = self.all(el, path)
        return found[0] if found else None

    def text(self, el, path: str) -> str | None:
        found = self.node(el, path)
        if found is None:
            return None
        value = found if isinstance(found, str) else found.text
        value = (value or "").strip()
        return value or None

    def dec(self, el, path: str) -> Decimal | None:
        return to_decimal(self.text(el, path))


def to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def to_date(value: str | None) -> date | None:
    """Accepts ISO dates (UBL) and the CII format 102 (YYYYMMDD)."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value[:10] if "-" in value else value[:8], fmt).date()
        except ValueError:
            continue
    return None
