"""
auth/routes.py
───────────────
REST endpoints for authentication.

POST /auth/register   → create account
POST /auth/login      → get JWT token
POST /auth/logout     → revoke token
GET  /auth/me         → get my profile + usage stats

How to register this in api/main.py:
    from auth.routes import router as auth_router
    app.include_router(auth_router, prefix="/auth", tags=["auth"])
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from auth.core import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from database.connection import get_db_for_fastapi
from database.models import Session as DBSession, User, UserPlan

router = APIRouter()


# ─────────────────────────────────────────────────────────────
#  Pydantic schemas — input/output validation
#  Pydantic checks the data before it touches our DB
#  Wrong email format? Rejected before it reaches our code.
# ─────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:        EmailStr          # validates email format automatically
    password:     str
    full_name:    str | None = None
    company_name: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      int
    email:        str
    plan:         str
    message:      str


class UserProfile(BaseModel):
    id:              int
    email:           str
    full_name:       str | None
    company_name:    str | None
    plan:            str
    files_used_today:  int
    files_used_month:  int
    daily_limit:     int
    monthly_limit:   int
    created_at:      datetime


# ─────────────────────────────────────────────────────────────
#  POST /auth/register
# ─────────────────────────────────────────────────────────────

@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db_for_fastapi)):
    """
    Create a new account.

    What happens:
      1. Check email not already taken
      2. Hash the password with bcrypt
      3. Create user record
      4. Create JWT token immediately (auto-login after signup)
      5. Store session
      6. Return token
    """
    # Check duplicate email
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please log in.",
        )

    # Create user
    user = User(
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        company_name=body.company_name,
        plan=UserPlan.free,
        is_active=True,
        files_used_today=0,
        files_used_month=0,
        last_reset_date=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()   # get the user.id without committing

    # Create token
    token, jti = create_access_token(user.id, user.plan.value)

    # Store session
    from auth.core import ACCESS_TOKEN_EXPIRE_HOURS
    from datetime import timedelta
    session = DBSession(
        user_id=user.id,
        jti=jti,
        is_revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    )
    db.add(session)
    db.commit()

    return AuthResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        plan=user.plan.value,
        message=f"Welcome! Your free account gives you 20 extractions per day.",
    )


# ─────────────────────────────────────────────────────────────
#  POST /auth/login
# ─────────────────────────────────────────────────────────────

@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest, db: Session = Depends(get_db_for_fastapi)):
    """
    Log in with email and password. Returns JWT token.

    Security note: we always say "invalid credentials" — never
    "email not found" or "wrong password" separately.
    Telling attackers which part is wrong helps them enumerate accounts.
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
    )

    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user:
        raise invalid

    if not verify_password(body.password, user.hashed_password):
        raise invalid

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Contact support.",
        )

    # Create token and store session
    token, jti = create_access_token(user.id, user.plan.value)

    from auth.core import ACCESS_TOKEN_EXPIRE_HOURS
    from datetime import timedelta
    session = DBSession(
        user_id=user.id,
        jti=jti,
        is_revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    )
    db.add(session)
    db.commit()

    plan_limits = {
        "free": "20 extractions/day",
        "starter": "100 extractions/month",
        "business": "500 extractions/month",
        "enterprise": "Unlimited",
    }

    return AuthResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        plan=user.plan.value,
        message=f"Welcome back! Plan: {plan_limits.get(user.plan.value, user.plan.value)}",
    )


# ─────────────────────────────────────────────────────────────
#  POST /auth/logout
# ─────────────────────────────────────────────────────────────

@router.post("/logout")
def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_for_fastapi),
):
    """
    Revoke the current JWT token.
    The token still exists but is_revoked=True so get_current_user
    rejects it on every future request.
    """
    # We need to get the jti from the token — it's in the DB session
    # We find it by user_id + not revoked + most recent
    session = db.query(DBSession).filter(
        DBSession.user_id == current_user.id,
        DBSession.is_revoked == False,
    ).order_by(DBSession.created_at.desc()).first()

    if session:
        session.is_revoked = True
        db.commit()

    return {"message": "Logged out successfully."}


# ─────────────────────────────────────────────────────────────
#  GET /auth/me
# ─────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserProfile)
def get_me(current_user: User = Depends(get_current_user)):
    """
    Returns the logged-in user's profile and usage stats.
    Frontend uses this to show the usage counter in the dashboard.
    """
    from auth.rate_limit import PLAN_LIMITS

    limits = PLAN_LIMITS.get(current_user.plan, PLAN_LIMITS[UserPlan.free])

    return UserProfile(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        company_name=current_user.company_name,
        plan=current_user.plan.value,
        files_used_today=current_user.files_used_today or 0,
        files_used_month=current_user.files_used_month or 0,
        daily_limit=limits["files"] if current_user.plan == UserPlan.free else 0,
        monthly_limit=limits["files"] if current_user.plan != UserPlan.free else 0,
        created_at=current_user.created_at,
    )