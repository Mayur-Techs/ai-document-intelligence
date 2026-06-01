"""
auth/core.py
─────────────
This file does three things:

1. PASSWORD HASHING
   Never store plain passwords. If your DB leaks, users are safe.
   bcrypt turns "mypassword123" into "$2b$12$xyz..." (irreversible)
   To verify: hash the input again and compare — never "unhash"

2. JWT CREATION
   After login, we create a token containing:
     - user_id  (who you are)
     - role     (what you can do)
     - jti      (unique token ID — used for logout)
     - exp      (expiry time — token dies after this)
   We sign it with a SECRET_KEY only our server knows.
   Anyone can READ the payload — but can't FAKE a valid signature.

3. JWT VERIFICATION
   Every protected request calls get_current_user().
   It reads the token from the HttpOnly access_token cookie,
   verifies the signature, checks expiry, checks not revoked.
   If all good — returns the User object.
   If anything fails — raises 401 Unauthorized.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from database.connection import get_db_for_fastapi
from database.models import Session as DBSession
from database.models import User

# ─────────────────────────────────────────────────────────────
#  Config — pulled from environment variables
#  Set JWT_SECRET_KEY in Render dashboard — long random string
#  Generate one: python -c "import secrets; print(secrets.token_hex(32))"
# ─────────────────────────────────────────────────────────────

_INSECURE_DEFAULT = "change-this-in-production-use-a-long-random-string"
SECRET_KEY = os.getenv("JWT_SECRET_KEY", _INSECURE_DEFAULT)
ALGORITHM = "HS256"  # HMAC SHA-256 — standard, fast, secure enough
ACCESS_TOKEN_EXPIRE_HOURS = 24  # token valid for 24 hours
_jwt_logger = logging.getLogger("docai.auth")

_JWT_SECRET = os.getenv("JWT_SECRET_KEY", "")
_INSECURE_DEFAULTS = {"secret", "changeme", "your-secret-key", "jwt-secret", ""}

if os.getenv("TESTING", "false").lower() != "true" and (
    not _JWT_SECRET or _JWT_SECRET.lower() in _INSECURE_DEFAULTS or len(_JWT_SECRET) < 32
):
    _jwt_logger.critical(
        "CRITICAL SECURITY: JWT_SECRET_KEY is missing or insecure. "
        'Generate a secure key: python -c "import secrets; print(secrets.token_hex(32))"'
    )

# ─────────────────────────────────────────────────────────────
#  Password hashing
#  Direct bcrypt is used instead of passlib for Python 3.12 compatibility.
# ─────────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ─────────────────────────────────────────────────────────────
#  JWT creation
# ─────────────────────────────────────────────────────────────


def create_access_token(user_id: int, role: str) -> tuple[str, str]:
    """
    Create a signed JWT token for a user.

    Returns:
        token — the JWT string to give to the frontend
        jti   — the unique token ID (stored in sessions table for revocation)

    Token payload contains:
        sub  — subject (user id as string, standard JWT field)
        role — user's plan/role
        jti  — unique id for this specific token
        exp  — expiry timestamp
        iat  — issued at timestamp
    """
    jti = str(uuid.uuid4())  # unique ID for this token
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)

    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "exp": expires,
        "iat": now,
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, jti


# ─────────────────────────────────────────────────────────────
#  JWT verification — FastAPI dependency
#  Usage: add  current_user: User = Depends(get_current_user)
#         to any route you want to protect
# ─────────────────────────────────────────────────────────────


def get_current_user(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db_for_fastapi),
) -> User:
    """
    FastAPI dependency — protects any route it's added to.
    Reads token from HttpOnly access_token cookie.
    If anything fails → 401 Unauthorized.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Please log in.",
    )

    if not access_token:
        raise credentials_error

    # Step 1 — decode and verify signature
    try:
        payload = jwt.decode(access_token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise credentials_error from None

    # Step 2 — extract fields
    user_id: str | None = payload.get("sub")
    jti: str | None = payload.get("jti")

    if not user_id or not jti:
        raise credentials_error

    # Step 3 — check token not revoked (logout support)
    session = (
        db.query(DBSession)
        .filter(
            DBSession.jti == jti,
            DBSession.is_revoked.is_(False),
            DBSession.expires_at > datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .first()
    )

    if not session:
        raise credentials_error

    # Step 4 — load user
    user = (
        db.query(User)
        .filter(
            User.id == int(user_id),
            User.is_active.is_(True),
        )
        .first()
    )

    if not user:
        raise credentials_error

    return user


def get_optional_user(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db_for_fastapi),
) -> User | None:
    """
    Same as get_current_user but returns None if cookie is missing/invalid.
    """
    if not access_token:
        return None
    try:
        return get_current_user(access_token, db)
    except HTTPException:
        return None
