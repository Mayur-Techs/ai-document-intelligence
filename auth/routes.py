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
from database.models import Session as DBSession
from database.models import User, UserPlan

router = APIRouter()


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


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str
    plan: str
    message: str


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

    # Generate email verification token
    import secrets
    verification_token = secrets.token_hex(32)
    user.verification_token = verification_token

    db.add(user)
    db.flush()  # get the user.id without committing

    # Create token
    token, jti = create_access_token(user.id, user.plan.value)

    # Store session
    from datetime import timedelta

    from auth.core import ACCESS_TOKEN_EXPIRE_HOURS

    session = DBSession(
        user_id=user.id,
        jti=jti,
        is_revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    )
    db.add(session)
    db.commit()

    # Send verification email if SMTP configured (non-blocking — registration succeeds either way)
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    frontend_url = os.getenv("FRONTEND_URL", "https://your-app.netlify.app")

    if smtp_host and smtp_user and smtp_pass:
        verify_link = f"{frontend_url}/verify-email.html?token={verification_token}&email={body.email}"
        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = body.email
        msg["Subject"] = "Verify your AI Document Intelligence account"
        body_text = (
            f"Welcome to AI Document Intelligence!\n\n"
            f"Please verify your email address by clicking the link below:\n\n"
            f"{verify_link}\n\n"
            f"This link does not expire.\n\n"
            f"— AI Document Intelligence"
        )
        msg.attach(MIMEText(body_text, "plain"))
        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, body.email, msg.as_string())
        except Exception:
            pass  # Don't block registration if email fails

    return AuthResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        plan=user.plan.value,
        message="Welcome! Your free account gives you 20 extractions per day.",
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

    from datetime import timedelta

    from auth.core import ACCESS_TOKEN_EXPIRE_HOURS

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
    session = (
        db.query(DBSession)
        .filter(
            DBSession.user_id == current_user.id,
            DBSession.is_revoked == False,  # noqa: E712  must use == for SQLAlchemy SQL generation
        )
        .order_by(DBSession.created_at.desc())
        .first()
    )

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
    import hashlib
    import os
    import secrets
    import smtplib
    from datetime import timedelta
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

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
        smtp_host = os.getenv("SMTP_HOST")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASSWORD")
        frontend_url = os.getenv("FRONTEND_URL", "https://your-app.netlify.app")

        if smtp_host and smtp_user and smtp_pass:
            reset_link = f"{frontend_url}/reset-password.html?token={raw_token}&email={body.email}"
            msg = MIMEMultipart()
            msg["From"] = smtp_user
            msg["To"] = body.email
            msg["Subject"] = "Reset your AI Document Intelligence password"
            body_text = (
                f"You requested a password reset.\n\n"
                f"Click the link below to reset your password (valid for 1 hour):\n\n"
                f"{reset_link}\n\n"
                f"If you didn't request this, you can safely ignore this email.\n\n"
                f"— AI Document Intelligence"
            )
            msg.attach(MIMEText(body_text, "plain"))
            try:
                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, body.email, msg.as_string())
            except Exception:
                pass  # Don't leak SMTP errors to the user

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
    import hashlib

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

