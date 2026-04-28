# AI Document Intelligence System

> **30-Day Financial Freedom Plan — System 2**
> Upload any PDF invoice, contract, or receipt. Claude Sonnet extracts structured fields automatically. Query, export, and verify via REST API.

[![CI](https://github.com/YOUR_USERNAME/ai-document-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/ai-document-intelligence/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-38%20passing-brightgreen.svg)]()

---

## What This Does

Drop a PDF → get structured JSON. That's it.

```
POST /api/v1/documents/upload  →  202 Accepted (job_id returned)
GET  /api/v1/documents/{id}    →  {"vendor": "Sharma Freight", "total": 197355, "confidence": 0.94, ...}
```

**Pipeline:**
```
PDF Upload → pdfplumber extract text → Claude Sonnet prompt → Pydantic validate
→ PostgreSQL store → FastAPI serve → n8n notify Slack
```

**n8n Google Drive workflow:** Drop PDF in folder → auto-extracted → Slack notification with vendor, invoice number, total amount, AI confidence score.

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/ai-document-intelligence.git
cd ai-document-intelligence
make setup              # copy .env.example → .env + install deps
nano .env               # add ANTHROPIC_API_KEY
make up                 # PostgreSQL + API + n8n on ports 8001/5433/5679
make migrate            # apply DB migrations
```

Test it immediately:
```bash
make upload-test FILE=your_invoice.pdf
make stats
```

---

## API Reference

Base URL: `http://localhost:8001` | Docs: `http://localhost:8001/docs`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/documents/upload` | Upload PDF, returns job_id (202) |
| `POST` | `/documents/batch/upload` | Upload multiple PDFs |
| `GET` | `/documents/{id}/status` | Poll processing status |
| `GET` | `/documents/{id}` | Full result + all fields |
| `GET` | `/documents/{id}/fields` | All extracted fields |
| `GET` | `/documents/` | List all, filter by status/type |
| `GET` | `/documents/stats/summary` | Aggregate stats |
| `GET` | `/documents/export` | CSV export |
| `GET` | `/documents/search?q=sharma` | Search extracted values |
| `POST` | `/documents/{id}/reprocess` | Re-run extraction |
| `PATCH` | `/documents/{id}/fields/{fid}/verify` | Mark field human-verified |
| `DELETE` | `/documents/{id}` | Delete document |
| `GET` | `/health` | Health check |

All routes at `/api/v1/`.

---

## Document Types Supported

| Type | Key Fields Extracted |
|------|---------------------|
| `invoice` | vendor, invoice#, date, due date, line items, subtotal, GST, total |
| `contract` | parties, effective date, term, key clauses |
| `receipt` | merchant, date, items, total, payment method |
| `report` | title, date, summary, key figures |

---

## Database Schema

Two tables:

**`documents`** — one row per uploaded file. 20 columns including `vendor_name`, `invoice_number`, `total_amount`, `ai_confidence` (0-1 float), `status` (queued/processing/completed/failed).

**`extracted_fields`** — one row per field extracted. `field_name`, `field_value` (string), `field_type`, `confidence`, `is_verified`. Flexible — any document type, no schema changes needed.

Migrations: `001_initial_schema.py`. Apply: `make migrate`.

---

## How Confidence Score Works

Claude returns `confidence_score: 0–100` with every extraction.
- **90–100**: All fields clearly visible, no ambiguity
- **60–89**: Some fields inferred or partially visible
- **0–59**: Significant uncertainty — flag for human review

Access it: `doc.ai_confidence` (stored as 0.0–1.0 float in DB).

---

## n8n Google Drive Workflow

Import `workflows/google_drive_pipeline_v1.json` at `http://localhost:5679`.

Nodes: Google Drive Trigger → Is PDF? → Download → Upload to API → Wait 20s → Get Result → Extraction Completed? → Format Slack Message → Slack Notification

Set env vars in n8n:
- `GOOGLE_DRIVE_FOLDER_ID` — folder to watch
- `SLACK_WEBHOOK_URL` — incoming webhook URL

---

## CLI Usage

```bash
# Extract + Claude + store to DB
python main.py --file invoice.pdf

# Dry run — extract text only, no Claude, no DB
python main.py --file invoice.pdf --dry-run

# Process full directory
python main.py --dir ./invoices/ --type invoice

# Specific document type
python main.py --file agreement.pdf --type contract
```

---

## Tests — 38 Passing

```bash
pytest tests/ -v        # no Docker required
make test-coverage      # HTML coverage report
```

`test_api.py` (14) — all 12 endpoints, SQLite StaticPool isolation  
`test_llm.py` (15) — Claude API mocked, all response paths including errors  
`test_parser.py` (9) — pdfplumber/fitz mocked, fallback logic, truncation  

---

## Deployment

```bash
git push origin main                    # CI runs tests + lint + docker build
# render.com → New → Blueprint → connect repo
# Render reads render.yaml → creates web service + PostgreSQL
# Add ANTHROPIC_API_KEY in Render Dashboard → Environment
# Live at: https://doc-intelligence.onrender.com/docs
```

---

## Connection to System 1 (Lead Gen)

System 1 finds logistics companies that import goods. System 2 reads their freight invoices and finds overcharges. Together they are FreightGuard's complete acquisition + delivery pipeline.

| System 1 does | System 2 does |
|---|---|
| Finds import/logistics companies | Audits their freight invoices |
| AI-qualifies by ICP fit | Extracts structured data from PDFs |
| Feeds outreach pipeline | Proves ROI to the customer |
