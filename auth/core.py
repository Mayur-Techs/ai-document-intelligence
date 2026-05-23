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
   It reads the token from the Authorization header,
   verifies the signature, checks expiry, checks not revoked.
   If all good — returns the User object.
   If anything fails — raises 401 Unauthorized.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database.connection import get_db_for_fastapi
from database.models import Session as DBSession, User

# ─────────────────────────────────────────────────────────────
#  Config — pulled from environment variables
#  Set JWT_SECRET_KEY in Render dashboard — long random string
#  Generate one: python -c "import secrets; print(secrets.token_hex(32))"
# ─────────────────────────────────────────────────────────────

SECRET_KEY       = os.getenv("JWT_SECRET_KEY", "change-this-in-production-use-a-long-random-string")
ALGORITHM        = "HS256"   # HMAC SHA-256 — standard, fast, secure enough
ACCESS_TOKEN_EXPIRE_HOURS = 24   # token valid for 24 hours

# ─────────────────────────────────────────────────────────────
#  Password hashing
#  CryptContext handles bcrypt automatically
# ─────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    """Turn 'mypassword' into '$2b$12$...' — one way, can't reverse."""
    return pwd_context.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    """Check if typed password matches stored hash. Returns True/False."""
    return pwd_context.verify(plain, hashed)


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
    jti     = str(uuid.uuid4())   # unique ID for this token
    now     = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)

    payload = {
        "sub":  str(user_id),
        "role": role,
        "jti":  jti,
        "exp":  expires,
        "iat":  now,
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, jti


# ─────────────────────────────────────────────────────────────
#  JWT verification — FastAPI dependency
#  Usage: add  current_user: User = Depends(get_current_user)
#         to any route you want to protect
# ─────────────────────────────────────────────────────────────

bearer_scheme = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db_for_fastapi),
) -> User:
    """
    FastAPI dependency — protects any route it's added to.

    Flow:
      1. Read token from "Authorization: Bearer <token>" header
      2. Decode and verify signature + expiry
      3. Check token not revoked in sessions table
      4. Load and return the User from DB

    If anything fails → 401 Unauthorized
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Please log in.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not credentials:
        raise credentials_error

    token = credentials.credentials

    # Step 1 — decode and verify signature
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise credentials_error

    # Step 2 — extract fields
    user_id: Optional[str] = payload.get("sub")
    jti:     Optional[str] = payload.get("jti")

    if not user_id or not jti:
        raise credentials_error

    # Step 3 — check token not revoked (logout support)
    session = db.query(DBSession).filter(
        DBSession.jti == jti,
        DBSession.is_revoked == False,
    ).first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or logged out. Please log in again.",
        )

    # Step 4 — load user
    user = db.query(User).filter(
        User.id == int(user_id),
        User.is_active == True,
    ).first()

    if not user:
        raise credentials_error

    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db_for_fastapi),
) -> Optional[User]:
    """
    Same as get_current_user but doesn't raise if not logged in.
    Returns None for anonymous users.
    Used on routes that work for both logged-in and anonymous users.
    Example: /upload works for both, but logged-in users get more files.
    """
    if not credentials:
        return None
    try:
        return get_current_user(credentials, db)
    except HTTPException:
        return None