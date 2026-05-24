"""
api/main.py — System 2: AI Document Intelligence API.

Follows identical patterns to System 1 (lead-gen-automation):
  - lifespan context manager (not deprecated @on_event)
  - structured logging setup at module load
  - CORSMiddleware from env var
  - versioned routes at /api/v1/
  - GET /health for Render + Docker healthchecks
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    yield
    logger.info("Shutting down AI Document Intelligence API.")


app = FastAPI(
    title="AI Document Intelligence API",
    debug=False,
    description=(
        "Upload PDF documents — invoices, contracts, receipts. "
        "Claude Sonnet extracts structured fields automatically. "
        "Query, export, and verify extracted data via REST API."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # ← critical for file downloads
)

app.include_router(export_router)
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(documents_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version, "service": "doc-intelligence"}
