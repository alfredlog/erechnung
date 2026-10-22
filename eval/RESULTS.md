# Extraction evaluation

Ground truth: the official KoSIT XRechnung test invoices (EUR only), rendered as plain PDFs
without embedded XML, and as 200-dpi grayscale scans for the OCR run.
Run with `python -m eval.run_eval` (add `--scan` for OCR, `--llm` with `LLM_API_KEY` set).

"Auto-approved" = the agent would have approved without a human. "WRONG" = approved although
a field needed for booking or paying was wrong. This is the number that must stay at 0.

## Rule-based extractor, text PDFs (86 invoices)

| Field | Correct |
|---|---|
| Invoice number | 76/86 (88.4 %) |
| Issue date | 86/86 (100 %) |
| Net total | 84/86 (97.7 %) |
| VAT total | 83/85 (97.6 %) |
| Gross total | 86/86 (100 %) |
| IBAN | 68/68 (100 %) |

Auto-approved correctly: **57** · auto-approved wrong: **0** · sent to review: 29

## Rule-based extractor + Tesseract OCR, scans (40 invoices)

| Field | Correct |
|---|---|
| Invoice number | 32/40 (80 %) |
| Issue date | 40/40 (100 %) |
| Net / VAT / gross | 100 % |
| IBAN | 35/35 (100 %) |

Auto-approved correctly: **27** · auto-approved wrong: **0** · sent to review: 13

(OCR run with the English Tesseract model in CI; the Docker image ships the German model.)

## What the evaluation changed

- Amounts in table layouts sit on the line after their label → reader looks one or two lines ahead.
- "Zahlbetrag" was taken as gross total when a rounding amount exists → gross and amount due separated.
- A computed value always "adds up", so arithmetic consistency raised the confidence of a value
  that was never in the document (2 wrong auto-approvals) → arithmetic can now only lower confidence.
- With OCR the seller name was a two-column heading → the seller is identified by VAT id or IBAN
  of a supplier a human confirmed, never by name.

## Limits

The test documents share one layout (ours). Real supplier invoices vary much more; the LLM
extractor is meant for those and must be measured on a set of real, anonymised invoices before
the automatic approval is switched on for PDFs at a customer.
