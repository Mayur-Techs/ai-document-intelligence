from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
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
    pool_pre_ping=True,   # verify connection health before use
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    logger.info("Initializing database schema…")
    Base.metadata.create_all(bind=engine)
    logger.info("Database ready.")


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
