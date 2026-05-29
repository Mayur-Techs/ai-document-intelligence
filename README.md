# AI Document Intelligence API

FastAPI backend for DocIntel AI. It accepts PDF invoices, stores the original
file, extracts structured invoice data with LLMs, verifies the extraction with
backend rules, saves the result in PostgreSQL, and exposes REST endpoints for
querying, export, feedback, and reprocessing.

## Production Pipeline

```text
PDF upload
-> FastAPI validation
-> S3 object storage, with local disk fallback
-> background extraction job
-> pdfplumber text extraction
-> Cerebras primary AI extraction
-> sanitizer and field cleanup
-> arithmetic verification of subtotal, tax, total, and line items
-> strict 0.99 confidence gate
-> Groq fallback only when verification/confidence/critical fields require it
-> PostgreSQL documents and extracted_fields tables
-> CSV, Excel, email, and stats endpoints
```

## Extraction Reliability

- Cerebras is the primary extraction provider.
- Groq is the fallback provider when the primary result is missing critical data
  or remains below the strict `0.99` confidence threshold.
- The backend does not blindly trust the LLM confidence number. After sanitizing
  the model output, it verifies invoice arithmetic:
  - `subtotal + tax_amount == total_amount`
  - sum of clean line-item amounts matches `subtotal`
- If the arithmetic and critical fields pass, the backend promotes
  `ai_confidence` to `1.0`.
- Groq rate limits are fail-fast. If Groq returns `429` or another rate-limit
  error, the fallback is skipped immediately so the processing pipeline does not
  hang.

## Important Production Rules

- Use `JWT_SECRET_KEY`, not `JWT_SECRET`.
- Auth uses an HttpOnly `access_token` cookie with `Secure` and `SameSite=None`.
- CORS must list explicit frontend origins when `allow_credentials=True`.
- S3 success must store an `s3://bucket/key` URI in `documents.file_path`.
- Local upload storage is only a fallback; Render free disk is ephemeral.
- Registered users must only access documents where `documents.user_id` matches their user id.
- Anonymous document actions must be scoped by the original IP address.
- Keep `CEREBRAS_API_KEY` configured. Without it, primary extraction cannot run.
- Keep `GROQ_API_KEY` configured for fallback, but the app will not block forever
  if Groq is rate-limited.

## Required Environment Variables

```env
DATABASE_URL=postgresql://...
JWT_SECRET_KEY=<64-char random secret>
CEREBRAS_API_KEY=<cloud.cerebras.ai key>
GROQ_API_KEY=<console.groq.com key>
GOOGLE_CLIENT_ID=<google oauth client id>
FRONTEND_URL=https://aidocli.netlify.app
AWS_ACCESS_KEY_ID=<iam access key>
AWS_SECRET_ACCESS_KEY=<iam secret>
AWS_STORAGE_BUCKET_NAME=doc-intel-uploads
AWS_S3_REGION_NAME=ap-south-1
UPLOAD_DIR=data/raw
LOG_LEVEL=INFO
```

Optional email features:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=<sender email>
SMTP_PASSWORD=<app password>
```

## Local Development

Use Python 3.12, matching the Dockerfile and production runtime.

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\ruff.exe check .
```

If `.\venv\Scripts\python.exe` fails with a missing Python path, delete and
recreate the venv with Python 3.12. A venv stores absolute interpreter paths.

Do not use Python 3.14 for local dependency verification. Some PDF packages used
by this backend may not have compatible wheels there yet, while Render and the
Dockerfile run Python 3.12.

## Render Deployment

Render uses `render.yaml` and the Dockerfile:

```text
runtime: docker
health check: /health
database: managed PostgreSQL
app port: 8000 inside the container
```

Before deploying, set these Render environment variables:

- `CEREBRAS_API_KEY`
- `GROQ_API_KEY`
- `GOOGLE_CLIENT_ID`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_STORAGE_BUCKET_NAME`
- `AWS_S3_REGION_NAME`
- `FRONTEND_URL`
- optional SMTP variables if email verification, reset, or export email is enabled

## Main Endpoints

All document routes are mounted under `/api/v1`.

```text
GET  /health
GET  /api/v1/stats
POST /api/v1/documents/upload
GET  /api/v1/documents/
GET  /api/v1/documents/{id}
GET  /api/v1/documents/{id}/status
GET  /api/v1/documents/{id}/fields
GET  /api/v1/documents/{id}/download
POST /api/v1/documents/{id}/reprocess
DELETE /api/v1/documents/{id}
POST /api/v1/documents/{id}/feedback
GET  /api/v1/documents/{id}/export/csv
GET  /api/v1/documents/{id}/export/excel
GET  /api/v1/documents/{id}/export/email?to=user@example.com
POST /auth/register
POST /auth/login
POST /auth/logout
GET  /auth/me
POST /auth/google
POST /auth/forgot-password
POST /auth/reset-password
POST /auth/verify-email
```

## Pre-Deploy Checklist

- `README.md`, `.env.example`, and `render.yaml` match the current backend env vars.
- The app starts with `uvicorn api.main:app`.
- `/health` returns `{"status": "ok"}`.
- Uploading a PDF creates a queued document and starts background extraction.
- A clean invoice with matching subtotal, tax, total, and line items can reach
  `ai_confidence = 1.0`.
- If Cerebras output is incomplete or below `0.99`, Groq fallback is attempted.
- If Groq is rate-limited, the pipeline skips fallback instead of freezing.
- S3 upload succeeds in production; local disk remains only a temporary fallback.
