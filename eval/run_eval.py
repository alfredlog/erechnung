"""Measures extraction quality field by field.

Ground truth: the official KoSIT XRechnung test invoices. Each one is rendered as a
plain PDF (no XML inside, like a typical PDF invoice from a small business) and,
with --scan, as an image (like a scanned paper invoice). The extractor must then
recover the key fields from that PDF/image alone.

    python -m eval.run_eval                 # rule-based extractor on PDFs
    python -m eval.run_eval --scan          # same, through OCR
    LLM_API_KEY=... python -m eval.run_eval --llm

Prints per-field accuracy and how many invoices the agent would have auto-approved
correctly vs. wrongly (the number that matters: a wrong auto-approval costs money).
"Correct" means every field needed for booking and paying is right; the supplier is
identified by VAT id or IBAN, as in the real agent.
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from app.core.config import settings
from app.domain.extract.grounding import AUTO_FIELDS, KEY_FIELDS, TRUST, field_confidence
from app.domain.extract.heuristic import extract_heuristic
from app.domain.extract.llm import extract_llm
from app.domain.extract.text import get_text
from app.domain.parse import parse_document
from app.domain.render.invoice_pdf import render_invoice_pdf
from app.domain.validate.rules import Severity, check_invoice

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "xrechnung"


def values(inv) -> dict:
    return {"number": inv.number, "issue_date": inv.issue_date, "seller_name": inv.seller.name,
            "net_total": inv.totals.net_total, "vat_total": inv.totals.vat_total,
            "gross_total": inv.totals.gross_total, "iban": inv.payment.iban}


def same(field: str, a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if field == "seller_name":
        return a.strip().lower() == b.strip().lower()
    if field == "iban":
        return a.replace(" ", "") == b.replace(" ", "")
    return a == b


def to_scan(pdf: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", "-singlefile", str(src), str(Path(tmp) / "p")],
                       check=True)
        return (Path(tmp) / "p.png").read_bytes()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(FIXTURES.glob("*.xml"))
    if args.limit:
        files = files[: args.limit]
    correct, present = Counter(), Counter()
    auto_ok = auto_wrong = review = 0
    for f in files:
        truth = parse_document(f.read_bytes())
        if truth.currency != "EUR":
            continue
        pdf = render_invoice_pdf(truth)
        data, is_pdf = (to_scan(pdf), False) if args.scan else (pdf, True)
        text = get_text(data, is_pdf).text
        if args.llm:
            got = extract_llm(text, base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                              model=settings.llm_model)
        else:
            got = extract_heuristic(text)
        rules = {x.code for x in check_invoice(got) if x.severity == Severity.ERROR}
        conf = field_confidence(got, text, rules)
        tv, gv = values(truth), values(got)
        for k in KEY_FIELDS:
            if tv[k] is None:
                continue
            present[k] += 1
            correct[k] += same(k, tv[k], gv[k])
        # As in the agent: the supplier counts as identified when its VAT id or IBAN matches
        # one already confirmed by a human (here: the true values, i.e. a returning supplier).
        identified = bool((got.seller.vat_id and got.seller.vat_id == (truth.seller.vat_id or "").replace(" ", ""))
                          or (got.payment.iban and got.payment.iban == truth.payment.iban))
        trusted = (identified and not rules
                   and all(conf.get(k, 0) >= TRUST for k in AUTO_FIELDS if tv[k] is not None))
        all_right = all(same(k, tv[k], gv[k]) for k in AUTO_FIELDS if tv[k] is not None)
        if trusted:
            auto_ok += all_right
            auto_wrong += not all_right
        else:
            review += 1

    mode = ("LLM" if args.llm else "rule-based") + (" + OCR" if args.scan else "")
    total = auto_ok + auto_wrong + review
    print(f"Extractor: {mode} — {total} invoices")
    for k in KEY_FIELDS:
        if present[k]:
            print(f"  {k:<12} {correct[k]:>3}/{present[k]:<3} {100 * correct[k] / present[k]:5.1f} %")
    print(f"Auto-approved correctly: {auto_ok}   auto-approved WRONG: {auto_wrong}   sent to review: {review}")


if __name__ == "__main__":
    main()
