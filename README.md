# E-Rechnung

Read, check and export German e-invoices (XRechnung, ZUGFeRD / Factur-X) for small businesses.
From 2027 German B2B invoices must be structured (EN 16931); this service turns them into something
a human can read, verifies them, and (next modules) exports them to DATEV.

**Status: week 1** – parsing + business checks + KoSIT integration + check page.

## What works today

- Format detection by content (not file name): UBL, CII, ZUGFeRD PDF, plain PDF, image
- Parsers for XRechnung UBL (Invoice + CreditNote), CII / ZUGFeRD 2.x / Factur-X, and XML embedded in PDF/A-3
- One format-independent model (`app/domain/models.py`, EN 16931 business terms)
- Business checks: totals (BR-CO-10/13/15/16), VAT breakdown, mandatory fields, IBAN checksum,
  XRechnung extension (third-party payments BG-DEX-09)
- Client for the official KoSIT validator (daemon mode, Docker service)
- `POST /api/v1/check` (JSON) and a German check page (`/`, HTMX, no external CDN)
- **Tested on all 87 official KoSIT XRechnung test-suite invoices** (`tests/fixtures/xrechnung`, Apache 2.0)

## Layout

```
app/
  api/         FastAPI routes + the "check" use case
  core/        settings
  domain/      pure business logic, no web/DB dependency
    models.py  Invoice, Party, Line, Totals (EN 16931)
    parse/     detect + UBL + CII + ZUGFeRD PDF
    validate/  rules.py (business checks), kosit_client.py
    generate/  (next) CII writer, PDF/A-3 embedding
    extract/   (next) PDF text, OCR, LLM extraction
    export/    (next) DATEV CSV
  worker/      (next) background jobs + agent
  web/         Jinja templates + static (htmx)
docker/kosit/  official validator + XRechnung rules
tests/         pytest, official fixtures
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
uvicorn app.api.main:app --reload      # http://localhost:8000
```

Without Docker the KoSIT check is skipped (shown on the page); everything else works.

## Run with Docker (VPS)

```bash
cp .env.example .env                    # set POSTGRES_PASSWORD
docker compose up -d --build
curl localhost:8100/health
```

The API listens on `127.0.0.1:8100` only; Nginx in front handles TLS and the domain.
Memory limits are set for a 4 GB VPS (api 512 MB, KoSIT 1 GB, Postgres 512 MB).

## Roadmap

1. Week 1 – parse + check ✅
2. Week 2 – free tools on PDF Libre (viewer, checker, ZUGFeRD creator)
3. Weeks 3–4 – accounts, upload inbox, invoice list, DATEV export → pilot customers
4. Then – PDF/scan extraction (LLM, EU-hosted), IMAP inbox, agent with human review
