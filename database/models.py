"""
database/models.py
───────────────────
All SQLAlchemy table definitions in one place.

Why SQLAlchemy?
  - We write Python classes, it generates SQL automatically
  - Works with PostgreSQL on Render and SQLite locally
  - One place to change a table — affects the whole app

Tables:
  users          — accounts (email + hashed password + plan)
  sessions       — active JWT tokens (for logout/revocation)
  ip_rate_limits — anonymous user request tracking by IP
  documents      — PDF uploads and extraction results
  extracted_fields — individual fields per document
  platform_stats — global counter
  feedback       — user ratings on extractions
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float,
    ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────
#  ENUM — user plans
#  Why enum? Prevents typos — can't accidentally write "busines"
# ─────────────────────────────────────────────────────────────

class UserPlan(str, enum.Enum):
    free       = "free"        # 5 files per day (IP-tracked before login)
    starter    = "starter"     # 100 files per month
    business   = "business"    # 500 files per month
    enterprise = "enterprise"  # unlimited
    admin      = "admin"       # internal use


# ─────────────────────────────────────────────────────────────
#  TABLE: users
#  One row per registered account
# ─────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name       = Column(String(255), nullable=True)
    company_name    = Column(String(255), nullable=True)
    plan            = Column(Enum(UserPlan), default=UserPlan.free, nullable=False)
    is_active       = Column(Boolean, default=True, nullable=False)
    is_verified     = Column(Boolean, default=False, nullable=False)  # email verified

    # Usage tracking
    files_used_today    = Column(Integer, default=0)
    files_used_month    = Column(Integer, default=0)
    last_reset_date     = Column(DateTime, nullable=True)  # when daily count was last reset

    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships — lets us do user.documents to get all their docs
    documents = relationship("Document", back_populates="user", lazy="select")
    sessions  = relationship("Session",  back_populates="user", lazy="select")
    feedbacks = relationship("Feedback", back_populates="user", lazy="select")

    def __repr__(self):
        return f"<User {self.email} plan={self.plan}>"


# ─────────────────────────────────────────────────────────────
#  TABLE: sessions
#  Why store sessions if using JWT?
#  JWT is stateless — you can't "log out" a JWT without a blocklist.
#  We store the JWT token ID (jti) here. On logout we mark it revoked.
#  Every request checks: is this token ID in the revoked list?
# ─────────────────────────────────────────────────────────────

class Session(Base):
    __tablename__ = "sessions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    jti        = Column(String(64), unique=True, index=True, nullable=False)  # JWT ID
    is_revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="sessions")


# ─────────────────────────────────────────────────────────────
#  TABLE: ip_rate_limits
#  Tracks anonymous users (not logged in) by IP address.
#  Rule: 5 free extractions per IP per 24 hours.
#  After that — show signup prompt.
# ─────────────────────────────────────────────────────────────

class IPRateLimit(Base):
    __tablename__ = "ip_rate_limits"

    id           = Column(Integer, primary_key=True, index=True)
    ip_address   = Column(String(45), unique=True, index=True, nullable=False)  # 45 = max IPv6 length
    request_count = Column(Integer, default=0, nullable=False)
    first_request = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_request  = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Auto-reset after 24 hours — checked in the rate limit logic
    window_start  = Column(DateTime, default=datetime.utcnow, nullable=False)


# ─────────────────────────────────────────────────────────────
#  TABLE: documents
#  Same as before + user_id foreign key added
# ─────────────────────────────────────────────────────────────

class Document(Base):
    __tablename__ = "documents"

    id           = Column(Integer, primary_key=True, index=True)

    # Who owns this document — NULL means anonymous (IP user)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ip_address   = Column(String(45), nullable=True)  # set for anonymous uploads

    file_name    = Column(String(500), nullable=False)
    file_path    = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    document_type   = Column(String(50), default="invoice")

    # Extraction results
    status          = Column(String(20), default="queued")
    page_count      = Column(Integer, nullable=True)
    char_count      = Column(Integer, nullable=True)
    vendor_name     = Column(String(500), nullable=True)
    invoice_number  = Column(String(200), nullable=True)
    invoice_date    = Column(String(50), nullable=True)
    due_date        = Column(String(50), nullable=True)
    total_amount    = Column(Float, nullable=True)
    currency        = Column(String(10), default="INR")
    ai_confidence   = Column(Float, nullable=True)
    error_message   = Column(Text, nullable=True)

    # Retention — anonymous docs deleted after 24hrs, user docs kept longer
    expires_at      = Column(DateTime, nullable=True)

    uploaded_at             = Column(DateTime, default=datetime.utcnow)
    processing_started_at   = Column(DateTime, nullable=True)
    processing_completed_at = Column(DateTime, nullable=True)

    user     = relationship("User",           back_populates="documents")
    fields   = relationship("ExtractedField", back_populates="document",
                            cascade="all, delete-orphan")
    feedbacks = relationship("Feedback",      back_populates="document")


# ─────────────────────────────────────────────────────────────
#  TABLE: extracted_fields
#  Unchanged from before — one row per extracted field
# ─────────────────────────────────────────────────────────────

class ExtractedField(Base):
    __tablename__ = "extracted_fields"

    id          = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    field_name  = Column(String(100), nullable=False)
    field_value = Column(Text, nullable=True)
    field_type  = Column(String(50), default="string")
    confidence  = Column(Float, nullable=True)
    is_verified = Column(Boolean, default=False)

    document = relationship("Document", back_populates="fields")


# ─────────────────────────────────────────────────────────────
#  TABLE: platform_stats
#  One row only (id=1) — global counter updated after every extraction
# ─────────────────────────────────────────────────────────────

class PlatformStats(Base):
    __tablename__ = "platform_stats"

    id               = Column(Integer, primary_key=True, default=1)
    total_documents  = Column(Integer, default=0)
    confidence_sum   = Column(Float, default=0.0)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
#  TABLE: feedback
#  Thumbs up/down + optional comment on each extraction
#  We use this weekly to improve prompts
# ─────────────────────────────────────────────────────────────

class Feedback(Base):
    __tablename__ = "feedback"

    id          = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    user_id     = Column(Integer, ForeignKey("users.id",     ondelete="SET NULL"), nullable=True)
    ip_address  = Column(String(45), nullable=True)
    rating      = Column(Integer, nullable=False)   # 1 = thumbs up, -1 = thumbs down
    comment     = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="feedbacks")
    user     = relationship("User",     back_populates="feedbacks")