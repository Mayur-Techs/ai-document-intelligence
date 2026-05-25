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

    # Self-healing database schema: add missing columns to documents if table already existed
    db = SessionLocal()
    try:
        logger.info("Verifying documents table schema and applying self-healing alters if needed...")
        db.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        db.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS ip_address VARCHAR(45)"))
        db.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"))

        # Self-healing: new user columns for email verification & password reset
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR(64)"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token VARCHAR(64)"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires_at TIMESTAMP"))

        # Try to add foreign key constraint. It might fail if already exists, which we catch.
        try:
            db.execute(text(
                "ALTER TABLE documents ADD CONSTRAINT fk_documents_user_id "
                "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL"
            ))
        except Exception:
            db.rollback()
            logger.info("Foreign key constraint fk_documents_user_id might already exist or users table was not ready.")

        db.commit()
        logger.info("Database schema verification and healing completed.")
    except Exception as exc:
        db.rollback()
        logger.error("Error during database schema healing: %s", exc)
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
