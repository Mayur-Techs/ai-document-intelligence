from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger("docai.database")

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql://docai:docai@localhost:5432/docai",
)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,  # verify connection health before use
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    logger.info("Initializing database schema…")
    Base.metadata.create_all(bind=engine)

    # Self-healing database schema: add missing columns to documents/users if tables already existed
    db = SessionLocal()
    
    # 1. Apply column additions first, committing each one independently.
    columns_to_add = [
        # documents table
        ("documents", "user_id", "INTEGER"),
        ("documents", "ip_address", "VARCHAR(45)"),
        ("documents", "expires_at", "TIMESTAMP"),
        # users table
        ("users", "is_verified", "BOOLEAN DEFAULT FALSE"),
        ("users", "verification_token", "VARCHAR(64)"),
        ("users", "reset_token", "VARCHAR(64)"),
        ("users", "reset_token_expires_at", "TIMESTAMP"),
    ]

    logger.info("Verifying table schemas and applying self-healing alters if needed...")
    for table, column, col_type in columns_to_add:
        try:
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"))
            db.commit()
            logger.info("Column %s.%s checked/added successfully.", table, column)
        except Exception as e:
            db.rollback()
            logger.warning("Could not check/add column %s.%s: %s", table, column, e)

    # 2. Try to add foreign key constraint in a separate transaction
    try:
        db.execute(text(
            "ALTER TABLE documents ADD CONSTRAINT fk_documents_user_id "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL"
        ))
        db.commit()
        logger.info("Foreign key constraint fk_documents_user_id verified/added.")
    except Exception as e:
        db.rollback()
        logger.info("Foreign key constraint fk_documents_user_id not added (it may already exist): %s", e)
    finally:
        db.close()



@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager for CLI/pipeline use. Auto-commits or rolls back."""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB transaction rolled back: %s", exc)
        raise
    finally:
        db.close()


def get_db_for_fastapi() -> Generator[Session, None, None]:
    """Generator for FastAPI Depends() — FastAPI manages the lifecycle."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
