# E-Rechnung

Receive, check, create and export German e-invoices (XRechnung, ZUGFeRD / Factur-X) for small
businesses. From 2027/2028 every German B2B invoice must be a structured e-invoice (EN 16931).
This service handles both directions:

- **Incoming:** an agent reads every invoice (XML, ZUGFeRD, plain PDF, scan, e-mail attachment),
  checks it and approves it on its own only when it is certain. Everything else goes to a human
  with a concrete reason ("IBAN changed", "amount not found in the document").
- **Outgoing:** write invoices in a form, get a PDF/A-3 ZUGFeRD file and the XRechnung XML,
  both validated against the official rules.
- **Export:** one ZIP for the tax advisor: DATEV Buchungsstapel, overview CSV, original documents.

## Architecture

```
            ┌──────────── Nginx (TLS, domain) ────────────┐
 browser ──►│  api (FastAPI + Jinja/HTMX)   :8100          │
            │     │ queue (jobs table)                     │
            │  worker  ── OCR (Tesseract) ── LLM (EU API)  │
            │     │ IMAP mailboxes                         │
            │  kosit (official validator, Java)            │
            │  db (PostgreSQL)    files volume (originals) │
            └──────────────────────────────────────────────┘
```

```
app/
  domain/              pure logic, no web/DB: easy to test
    models.py          one invoice model (EN 16931 business terms) for every format
    parse/             detect format by content; UBL, CII, XML inside ZUGFeRD PDFs
    validate/          arithmetic + mandatory fields + IBAN; client for the KoSIT validator
    generate/          form data -> totals -> CII XML -> PDF/A-3 with embedded XML
    extract/           text from PDF, OCR, rule-based and LLM extraction, grounding check
    render/            readable PDF of any invoice (embedded fonts)
    export/            DATEV Buchungsstapel (EXTF 700)
  services/            use cases with the database
    agent.py           receive -> read -> check -> decide (the approval policy)
    jobs.py            work queue in PostgreSQL (FOR UPDATE SKIP LOCKED)
    mail.py            IMAP inbox polling
    invoicing.py       outgoing invoices, numbering, archive
    exports.py         ZIP for the tax advisor
    accounts.py        registration, login, sessions
  api/                 routes: public tools, auth, workspace
  worker/              background process
  web/                 templates + static files (htmx served locally, no CDN)
eval/                  extraction benchmark (see eval/RESULTS.md)
tests/                 139 tests incl. the 87 official KoSIT test invoices
```

## The agent's decision policy

An invoice is approved automatically only if **all** of these hold:

1. Structured e-invoice, or every value needed for booking and paying (number, date, net, VAT,
   gross, IBAN) was found literally in the document and the amounts add up. An LLM answer that
   is not in the document is not trusted ("grounding").
2. No arithmetic or mandatory-field errors; official KoSIT validation passed (for XML).
3. The seller is a supplier a human already confirmed, identified by **VAT id** (or IBAN if the
   invoice has no VAT id), never by name.
4. The IBAN is the one confirmed for this supplier. A changed IBAN (a common fraud) blocks the
   invoice and needs an explicit "confirmed by phone" to approve.
5. Not a duplicate (same number and same supplier, sender name or amount, e.g. e-mail + scan).
6. Not a credit note; automatic approval switched on for the company.

Every step is written to an append-only audit log (who, what, when, old → new).

## Measured

- Parsing: all 87 official KoSIT XRechnung test invoices (UBL + CII), 0 rule errors.
- Generation: XRechnung and ZUGFeRD XML accepted by the official KoSIT validator 1.6.3 with
  XRechnung 3.0.2 rules (standard and small-business variants); CII XSD valid.
- Extraction (rules only, no LLM): 0 wrong automatic approvals on 86 PDFs and 40 scans; see
  [eval/RESULTS.md](eval/RESULTS.md).

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                               # add KOSIT_TEST_URL=http://127.0.0.1:8099/ to use a local KoSIT
INLINE_JOBS=1 uvicorn app.api.main:app --reload    # http://localhost:8000, SQLite, no worker needed
```

## Deploy (VPS, Docker)

See [DEPLOY.md](DEPLOY.md). In short: `cp .env.example .env`, fill it, `docker compose up -d --build`.

## Security and data protection

- Servers in Germany; LLM optional and configurable (default: Mistral, EU). Without a key the
  rule-based reader is used and no invoice leaves the server.
- Passwords hashed with scrypt; only hashes of session tokens stored; mailbox passwords encrypted.
- Cookies HttpOnly, SameSite=Lax (blocks cross-site form posts), Secure in production.
- Every query is scoped to the user's company; tested.
- Original files stored read-only under their SHA-256, verified on every read (GoBD).

## Known limits / next steps

- DATEV export follows the EXTF 700 format with SKR03 defaults: do a test import with the tax
  advisor before real use.
- PDF/A-3 properties are checked in tests (fonts embedded, output intent, XMP, attachment), but
  not yet with veraPDF.
- LLM extraction is tested with a mocked API; measure it on real anonymised invoices.
- Not yet: payments/subscriptions (Stripe), several users per company, tax-advisor access,
  Peppol sending, Alembic migrations.
