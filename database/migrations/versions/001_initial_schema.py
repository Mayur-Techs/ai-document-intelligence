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
    # ── users ────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("plan", sa.Enum("free", "starter", "business", "enterprise", "admin", name="userplan"), nullable=False, server_default="free"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("files_used_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_used_month", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reset_date", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_id", "users", ["id"], unique=False)

    # ── sessions ──────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_sessions_id", "sessions", ["id"], unique=False)
    op.create_index("ix_sessions_jti", "sessions", ["jti"], unique=True)

    # ── ip_rate_limits ───────────────────────────────────────────────
    op.create_table(
        "ip_rate_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_request", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_request", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("window_start", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_ip_rate_limits_id", "ip_rate_limits", ["id"], unique=False)
    op.create_index("ix_ip_rate_limits_ip_address", "ip_rate_limits", ["ip_address"], unique=True)

    # ── documents ─────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("document_type", sa.String(length=50), nullable=False, server_default="invoice"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("vendor_name", sa.String(length=500), nullable=True),
        sa.Column("invoice_number", sa.String(length=200), nullable=True),
        sa.Column("invoice_date", sa.String(length=50), nullable=True),
        sa.Column("due_date", sa.String(length=50), nullable=True),
        sa.Column("total_amount", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="INR"),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processing_started_at", sa.DateTime(), nullable=True),
        sa.Column("processing_completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_documents_id", "documents", ["id"], unique=False)

    # ── extracted_fields ──────────────────────────────────────────────
    op.create_table(
        "extracted_fields",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(length=100), nullable=False),
        sa.Column("field_value", sa.Text(), nullable=True),
        sa.Column("field_type", sa.String(length=50), nullable=False, server_default="string"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_extracted_fields_id", "extracted_fields", ["id"], unique=False)

    # ── platform_stats ────────────────────────────────────────────────
    op.create_table(
        "platform_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("total_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence_sum", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id")
    )

    # ── feedback ──────────────────────────────────────────────────────
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_feedback_id", "feedback", ["id"], unique=False)


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("platform_stats")
    op.drop_table("extracted_fields")
    op.drop_table("documents")
    op.drop_table("ip_rate_limits")
    op.drop_table("sessions")
    op.drop_table("users")
    # Drop type in postgres
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE userplan")
