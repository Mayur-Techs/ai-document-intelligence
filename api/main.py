"""
api/main.py — System 2: AI Document Intelligence API.

Follows identical patterns to System 1 (lead-gen-automation):
  - lifespan context manager (not deprecated @on_event)
  - structured logging setup at module load
  - explicit CORSMiddleware origins for credentialed browser requests
  - versioned routes at /api/v1/
  - GET /health for Render + Docker healthchecks
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import extractor.pipeline
from api.export import router as export_router
from api.routes.documents import router as documents_router
from auth.routes import router as auth_router
from database.connection import init_db
from utils.logger import setup_logging

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("docai.api")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    logger.info("Starting AI Document Intelligence API v%s...", app.version)
    init_db()
    # Ensure upload directory exists
    upload_dir = os.getenv("UPLOAD_DIR", "data/raw")
    os.makedirs(upload_dir, exist_ok=True)
    logger.info("API ready. Upload dir: %s | Docs: /docs", upload_dir)

    # Initialize and start single-worker background queue task (only in production, not in tests)
    if os.getenv("TESTING") != "true":
        extractor.pipeline.QUEUE_ACTIVE = True
        app.state.worker_task = asyncio.create_task(extractor.pipeline.document_worker())
        logger.info("[QUEUE] Single-worker background queue worker started.")

    # Production readiness checks — warn loudly in logs if env vars are missing
    if not os.getenv("SMTP_HOST"):
        logger.warning(
            "SMTP_HOST not set — email verification, password reset, and document email-export "
            "are DISABLED. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD in Render environment variables."
        )
    if not os.getenv("AWS_STORAGE_BUCKET_NAME"):
        logger.warning(
            "AWS_STORAGE_BUCKET_NAME not set — PDF files will be stored on Render's ephemeral "
            "disk and DELETED on every redeploy. Set S3/R2 credentials to enable persistent storage."
        )
    if (
        os.getenv("JWT_SECRET_KEY", "change-this-in-production-use-a-long-random-string")
        == "change-this-in-production-use-a-long-random-string"
    ):
        logger.warning(
            "JWT_SECRET_KEY is using the default insecure value! "
            "Set a strong random JWT_SECRET_KEY in Render environment variables immediately."
        )

    yield
    # Clean up background worker task
    if hasattr(app.state, "worker_task"):
        logger.info("[QUEUE] Cancelling single-worker background queue worker task...")
        app.state.worker_task.cancel()
        try:
            await app.state.worker_task
        except asyncio.CancelledError:
            logger.info("[QUEUE] Single-worker background queue worker task cancelled successfully.")
    logger.info("Shutting down AI Document Intelligence API.")


app = FastAPI(
    title="AI Document Intelligence API",
    debug=False,
    description=(
        "Upload PDF documents — invoices, contracts, receipts. "
        "Cerebras and Groq extract structured fields automatically. "
        "Query, export, and verify extracted data via REST API."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://aidocli.netlify.app",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(export_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(documents_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version, "service": "doc-intelligence"}
