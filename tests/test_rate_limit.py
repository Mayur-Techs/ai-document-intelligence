from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth.rate_limit import IP_LIMIT, check_ip_rate_limit, check_user_plan_limit
from database.models import Base, IPRateLimit, User, UserPlan


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_ip_rate_limit_counts_batch_amount(db_session):
    check_ip_rate_limit("203.0.113.10", db_session, amount=3)

    record = db_session.query(IPRateLimit).filter_by(ip_address="203.0.113.10").one()
    assert record.request_count == 3


def test_ip_rate_limit_rejects_batch_that_exceeds_remaining_quota(db_session):
    check_ip_rate_limit("203.0.113.11", db_session, amount=IP_LIMIT - 1)

    with pytest.raises(HTTPException) as exc:
        check_ip_rate_limit("203.0.113.11", db_session, amount=2)

    assert exc.value.status_code == 429


def test_ip_rate_limit_rejects_first_batch_over_total_quota(db_session):
    with pytest.raises(HTTPException) as exc:
        check_ip_rate_limit("203.0.113.12", db_session, amount=IP_LIMIT + 1)

    assert exc.value.status_code == 429


def test_user_plan_limit_counts_batch_amount():
    user = User(
        email="quota@example.com",
        hashed_password="not-used",
        plan=UserPlan.free,
        files_used_today=18,
        files_used_month=18,
    )
    db = Mock()

    check_user_plan_limit(user, db, amount=2)

    assert user.files_used_today == 20
    assert user.files_used_month == 20


def test_user_plan_limit_rejects_batch_that_exceeds_remaining_quota():
    user = User(
        email="quota@example.com",
        hashed_password="not-used",
        plan=UserPlan.free,
        files_used_today=19,
        files_used_month=19,
    )
    db = Mock()

    with pytest.raises(HTTPException) as exc:
        check_user_plan_limit(user, db, amount=2)

    assert exc.value.status_code == 429
