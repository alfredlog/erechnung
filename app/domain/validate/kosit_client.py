"""Client for the official KoSIT validator running in daemon mode (docker service "kosit").

The daemon answers POST / with the XML in the body:
  200 -> acceptable, 406 -> not acceptable; the body is the XML validation report.
"""
from __future__ import annotations

import httpx
from lxml import etree
from pydantic import BaseModel

from app.domain.parse._xml import parse_xml

REP_NS = {"rep": "http://www.xoev.de/de/validator/varl/1", "svrl": "http://purl.oclc.org/dsdl/svrl"}


class KositMessage(BaseModel):
    level: str          # error / warning / information
    code: str | None
    text: str


class KositResult(BaseModel):
    acceptable: bool
    messages: list[KositMessage]
    report_xml: str


def _messages(report: bytes) -> list[KositMessage]:
    try:
        root = parse_xml(report)
    except etree.XMLSyntaxError:
        return []
    out = []
    # Schematron failures (business rules such as BR-DE-15)
    for fa in root.xpath("//svrl:failed-assert", namespaces=REP_NS):
        out.append(KositMessage(
            level="warning" if fa.get("flag") == "warning" else "error",
            code=fa.get("id"),
            text=" ".join("".join(fa.xpath("svrl:text//text()", namespaces=REP_NS)).split()),
        ))
    # XML schema errors
    for msg in root.xpath("//rep:message", namespaces=REP_NS):
        out.append(KositMessage(level=msg.get("level", "error"), code=msg.get("code"), text=(msg.text or "").strip()))
    return out


def validate_xml(xml: bytes, base_url: str, timeout: float = 30.0) -> KositResult:
    r = httpx.post(base_url, content=xml, headers={"Content-Type": "application/xml"}, timeout=timeout)
    if r.status_code not in (200, 406):
        r.raise_for_status()
        raise RuntimeError(f"Unexpected KoSIT status {r.status_code}")
    return KositResult(acceptable=r.status_code == 200, messages=_messages(r.content), report_xml=r.text)
