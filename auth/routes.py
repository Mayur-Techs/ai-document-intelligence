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

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from auth.core import (
    create_access_token,
    get_current_user,
    get_optional_user,
    hash_password,
    verify_password,
)
from database.connection import get_db_for_fastapi
from database.models import Session as DBSession
from database.models import User, UserPlan
from utils.email import is_email_configured, send_email

router = APIRouter()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
COOKIE_MAX_AGE_SECONDS = 86400


def _session_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=COOKIE_MAX_AGE_SECONDS)


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=COOKIE_MAX_AGE_SECONDS,
        path="/",
    )


# ─────────────────────────────────────────────────────────────
#  Pydantic schemas — input/output validation
#  Pydantic checks the data before it touches our DB
#  Wrong email format? Rejected before it reaches our code.
# ─────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    email: EmailStr  # validates email format automatically
    password: str
    full_name: str | None = None
    company_name: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserProfile(BaseModel):
    id: int
    email: str
    full_name: str | None
    company_name: str | None
    plan: str
    files_used_today: int
    files_used_month: int
    daily_limit: int
    monthly_limit: int
    created_at: datetime


# ─────────────────────────────────────────────────────────────
#  POST /auth/register
# ─────────────────────────────────────────────────────────────


@router.post("/register", status_code=201)
def register(
    body: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db_for_fastapi),
):
    """
    Create a new account.

    What happens:
      1. Check email not already taken
      2. Hash the password with bcrypt
      3. Create user record
      4. Create JWT token immediately (auto-login after signup)
      5. Store session in DB
      6. Set HttpOnly secure cookie on the response
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

    # Generate email verification token
    import secrets

    verification_token = secrets.token_hex(32)
    user.verification_token = verification_token

    db.add(user)
    db.flush()  # get the user.id without committing

    # Create token
    token, jti = create_access_token(user.id, user.plan.value)

    # Store session
    session = DBSession(
        user_id=user.id,
        jti=jti,
        is_revoked=False,
        expires_at=_session_expires_at(),
    )
    db.add(session)
    db.commit()

    # Send verification email if SMTP configured (non-blocking — registration succeeds either way)
    if is_email_configured():
        frontend_url = os.getenv("FRONTEND_URL", "https://your-app.netlify.app")
        verify_link = (
            f"{frontend_url}/verify-email.html?token={verification_token}&email={body.email}"
        )
        body_text = (
            f"Welcome to AI Document Intelligence!\n\n"
            f"Please verify your email address by clicking the link below:\n\n"
            f"{verify_link}\n\n"
            f"This link does not expire.\n\n"
            f"— AI Document Intelligence"
        )
        send_email(
            to=body.email,
            subject="Verify your AI Document Intelligence account",
            body=body_text,
        )

    _set_auth_cookie(response, token)

    return {
        "message": "Welcome! Your free account gives you 20 extractions per day.",
        "email": user.email,
        "plan": user.plan.value,
    }


# ─────────────────────────────────────────────────────────────
#  POST /auth/login
# ─────────────────────────────────────────────────────────────


@router.post("/login")
def login(
    body: LoginRequest,
    response: Response,
    db: Session = Depends(get_db_for_fastapi),
):
    """
    Log in with email and password. Sets HttpOnly cookie.
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

    session = DBSession(
        user_id=user.id,
        jti=jti,
        is_revoked=False,
        expires_at=_session_expires_at(),
    )
    db.add(session)
    db.commit()

    plan_limits = {
        "free": "20 extractions/day",
        "starter": "100 extractions/month",
        "business": "500 extractions/month",
        "enterprise": "Unlimited",
    }

    _set_auth_cookie(response, token)

    return {
        "message": f"Welcome back! Plan: {plan_limits.get(user.plan.value, user.plan.value)}",
        "email": user.email,
        "plan": user.plan.value,
    }


# ─────────────────────────────────────────────────────────────
#  POST /auth/logout
# ─────────────────────────────────────────────────────────────


@router.post("/logout")
def logout(
    response: Response,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
):
    """
    Revoke current JWT session and clear the HttpOnly access_token cookie.
    """
    if current_user:
        session = (
            db.query(DBSession)
            .filter(
                DBSession.user_id == current_user.id,
                DBSession.is_revoked.is_(False),
            )
            .order_by(DBSession.created_at.desc())
            .first()
        )
        if session:
            session.is_revoked = True
            db.commit()

    response.delete_cookie(
        key="access_token",
        path="/",
        secure=True,
        samesite="none",
        httponly=True,
    )
    return {"message": "Logged out"}


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


# ─────────────────────────────────────────────────────────────
#  POST /auth/forgot-password
#  Generates a one-time reset token and emails it.
#  Security: we always return 200 regardless of whether email exists.
#  This prevents account enumeration attacks.
# ─────────────────────────────────────────────────────────────


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db_for_fastapi)):
    """
    Request a password reset link via email.

    Always returns 200 — never reveals whether the email is registered.
    If SMTP is configured, the user receives a reset link valid for 1 hour.
    """
    # Always respond 200 to prevent account enumeration
    user = db.query(User).filter(User.email == body.email.lower()).first()

    if user:
        # Generate a cryptographically secure token
        raw_token = secrets.token_hex(32)
        # Store only the SHA-256 hash — raw token stays only in the email
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        user.reset_token = token_hash
        user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()

        # Send reset email if SMTP configured
        if is_email_configured():
            frontend_url = os.getenv("FRONTEND_URL", "https://your-app.netlify.app")
            reset_link = f"{frontend_url}/reset-password.html?token={raw_token}&email={body.email}"
            body_text = (
                f"You requested a password reset.\n\n"
                f"Click the link below to reset your password (valid for 1 hour):\n\n"
                f"{reset_link}\n\n"
                f"If you didn't request this, you can safely ignore this email.\n\n"
                f"— AI Document Intelligence"
            )
            send_email(
                to=body.email,
                subject="Reset your AI Document Intelligence password",
                body=body_text,
            )

    return {"message": "If that email is registered, a reset link has been sent."}


# ─────────────────────────────────────────────────────────────
#  POST /auth/reset-password
#  Validates the token and sets the new password.
# ─────────────────────────────────────────────────────────────


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    token: str  # the raw token from the email link
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db_for_fastapi)):
    """
    Reset the password using the one-time token from the email link.
    Token is valid for 1 hour and can only be used once.
    """

    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired reset token.",
    )

    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not user.reset_token or not user.reset_token_expires_at:
        raise invalid

    # Check expiry
    expires = user.reset_token_expires_at
    # Make timezone-aware for comparison
    if expires.tzinfo is None:
        from datetime import timezone as _tz

        expires = expires.replace(tzinfo=_tz.utc)
    if datetime.now(timezone.utc) > expires:
        raise invalid

    # Compare hash of submitted token against stored hash
    submitted_hash = hashlib.sha256(body.token.encode()).hexdigest()
    if submitted_hash != user.reset_token:
        raise invalid

    # All checks passed — set new password and clear token
    user.hashed_password = hash_password(body.new_password)
    user.reset_token = None
    user.reset_token_expires_at = None
    db.commit()

    return {"message": "Password reset successfully. You can now log in with your new password."}


# ─────────────────────────────────────────────────────────────
#  POST /auth/verify-email
#  Validates the email verification token sent on signup.
# ─────────────────────────────────────────────────────────────


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    token: str


@router.post("/verify-email")
def verify_email(body: VerifyEmailRequest, db: Session = Depends(get_db_for_fastapi)):
    """
    Verify email address using the token sent on registration.
    Marks the account as is_verified=True and clears the token.
    """
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or already-used verification token.",
    )

    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not user.verification_token:
        raise invalid

    if user.verification_token != body.token:
        raise invalid

    user.is_verified = True
    user.verification_token = None
    db.commit()

    return {"message": "Email verified successfully. Your account is now active."}


# ─────────────────────────────────────────────────────────────
#  POST /auth/google
# ─────────────────────────────────────────────────────────────


@router.post("/google")
def google_login(
    body: dict,
    response: Response,
    db: Session = Depends(get_db_for_fastapi),
):
    """
    Receives Google credential token from frontend.
    Verifies it with Google. Creates or finds user.
    Sets HttpOnly cookie. Returns user info.
    """
    credential = body.get("credential")
    if not credential:
        raise HTTPException(status_code=400, detail="Missing credential")

    try:
        id_info = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except ValueError as err:
        raise HTTPException(status_code=401, detail="Invalid Google token") from err

    email = id_info["email"]
    full_name = id_info.get("name", "")

    # Find or create user
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            email=email,
            hashed_password=hash_password(os.urandom(32).hex()),
            full_name=full_name,
            plan=UserPlan.free,
            is_active=True,
            is_verified=True,
            files_used_today=0,
            files_used_month=0,
            last_reset_date=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Contact support.",
        )

    token, jti = create_access_token(user.id, user.plan.value)

    # Store session
    session = DBSession(
        user_id=user.id,
        jti=jti,
        is_revoked=False,
        expires_at=_session_expires_at(),
    )
    db.add(session)
    db.commit()

    _set_auth_cookie(response, token)
    return {
        "message": "Login successful",
        "email": user.email,
        "plan": user.plan.value,
        "full_name": user.full_name,
    }
