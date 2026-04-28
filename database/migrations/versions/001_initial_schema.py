"""initial_schema

Revision ID: 001a2b3c4d5e
Revises:
Create Date: 2026-04-30 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001a2b3c4d5e"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── documents ────────────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("stored_path", sa.String(1000), nullable=False),
        sa.Column("file_size_bytes", sa.Integer),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("document_type", sa.String(50), server_default="invoice"),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("page_count", sa.Integer),
        sa.Column("raw_text_length", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("uploaded_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("processed_at", sa.DateTime),
    )
    op.create_index("ix_documents_id", "documents", ["id"])
    op.create_index("ix_documents_status", "documents", ["status"])

    # ── invoice_extractions ───────────────────────────────────────────────────
    op.create_table(
        "invoice_extractions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), unique=True, nullable=False),
        sa.Column("invoice_number", sa.String(200)),
        sa.Column("invoice_date", sa.String(50)),
        sa.Column("due_date", sa.String(50)),
        sa.Column("currency", sa.String(10), server_default="INR"),
        sa.Column("vendor_name", sa.String(500)),
        sa.Column("vendor_address", sa.Text),
        sa.Column("vendor_gstin", sa.String(20)),
        sa.Column("buyer_name", sa.String(500)),
        sa.Column("buyer_address", sa.Text),
        sa.Column("buyer_gstin", sa.String(20)),
        sa.Column("subtotal", sa.Float),
        sa.Column("tax_amount", sa.Float),
        sa.Column("discount_amount", sa.Float),
        sa.Column("total_amount", sa.Float),
        sa.Column("line_items", sa.JSON),
        sa.Column("confidence_score", sa.Integer),
        sa.Column("model_used", sa.String(100)),
        sa.Column("tokens_used", sa.Integer),
        sa.Column("raw_llm_response", sa.Text),
        sa.Column("extracted_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_invoice_extractions_id", "invoice_extractions", ["id"])
    op.create_index("ix_invoice_extractions_document_id", "invoice_extractions", ["document_id"])


def downgrade() -> None:
    op.drop_table("invoice_extractions")
    op.drop_table("documents")
