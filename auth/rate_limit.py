"""
auth/rate_limit.py
───────────────────
Two types of limits:

1. IP RATE LIMIT (anonymous users — not logged in)
   - 5 free extractions per IP per 24 hours
   - Tracked in ip_rate_limits table
   - After limit → return 429 with signup prompt message

2. PLAN LIMIT (logged-in users)
   - free:       20 files per day
   - starter:    100 files per month
   - business:   500 files per month
   - enterprise: unlimited

Why 24-hour windows and not calendar day?
   Simpler. If you upload at 11pm, your window resets at 11pm tomorrow.
   Calendar day resets at midnight — confusing for users in different timezones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from database.models import IPRateLimit, User, UserPlan

# ─────────────────────────────────────────────────────────────
#  Limits per plan
# ─────────────────────────────────────────────────────────────

PLAN_LIMITS = {
    UserPlan.free: {"files": 20, "window_hours": 24},
    UserPlan.starter: {"files": 100, "window_hours": 720},  # 30 days
    UserPlan.business: {"files": 500, "window_hours": 720},
    UserPlan.enterprise: {"files": 999999, "window_hours": 720},
    UserPlan.admin: {"files": 999999, "window_hours": 24},
}

IP_LIMIT = 5  # anonymous users: 5 files per 24 hours
IP_WINDOW_HOURS = 24


# ─────────────────────────────────────────────────────────────
#  Get real IP address
#  Render sits behind a proxy — the real IP is in X-Forwarded-For header
# ─────────────────────────────────────────────────────────────


def get_client_ip(request: Request) -> str:
    """
    Get real IP address. Works behind Render's proxy.

    X-Forwarded-For header contains: "client_ip, proxy1_ip, proxy2_ip"
    We always take the FIRST one — that's the real client.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─────────────────────────────────────────────────────────────
#  IP rate limiting — for anonymous users
# ─────────────────────────────────────────────────────────────


def check_ip_rate_limit(ip: str, db: Session) -> None:
    """
    Check if this IP has exceeded the anonymous limit.
    Creates a record on first visit, increments on each call.
    Resets after 24 hours.

    Raises 429 if limit exceeded.
    """
    now = datetime.now(timezone.utc)
    record = db.query(IPRateLimit).filter(IPRateLimit.ip_address == ip).first()

    if not record:
        # First time this IP visits — create record
        record = IPRateLimit(
            ip_address=ip,
            request_count=1,
            first_request=now,
            last_request=now,
            window_start=now,
        )
        db.add(record)
        db.commit()
        return  # first request always allowed

    # Check if 24-hour window has passed — if so, reset
    window_age = now - record.window_start.replace(tzinfo=timezone.utc)
    if window_age > timedelta(hours=IP_WINDOW_HOURS):
        record.request_count = 1
        record.window_start = now
        record.last_request = now
        db.commit()
        return  # reset and allow

    # Within window — check count
    if record.request_count >= IP_LIMIT:
        reset_at = record.window_start.replace(tzinfo=timezone.utc) + timedelta(
            hours=IP_WINDOW_HOURS
        )
        minutes_left = int((reset_at - now).total_seconds() / 60)

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": f"Free limit reached. {IP_LIMIT} extractions per 24 hours for anonymous users.",
                "resets_in_minutes": minutes_left,
                "action": "Create a free account for 20 extractions per day.",
                "signup_url": "/signup",
            },
        )

    # Under limit — increment and allow
    record.request_count += 1
    record.last_request = now
    db.commit()


# ─────────────────────────────────────────────────────────────
#  User plan limit — for logged-in users
# ─────────────────────────────────────────────────────────────


def check_user_plan_limit(user: User, db: Session) -> None:
    """
    Check if a logged-in user has hit their plan limit.
    Resets daily for free plan, monthly for paid plans.

    Raises 429 if limit exceeded with upgrade prompt.
    """
    now = datetime.now(timezone.utc)
    limits = PLAN_LIMITS.get(user.plan, PLAN_LIMITS[UserPlan.free])

    # Check if window needs resetting
    if user.last_reset_date:
        last_reset = user.last_reset_date.replace(tzinfo=timezone.utc)
        window_age = now - last_reset

        if window_age > timedelta(hours=limits["window_hours"]):
            # Reset the counter
            user.files_used_today = 0
            user.files_used_month = 0
            user.last_reset_date = now
            db.commit()

    # First ever upload
    if not user.last_reset_date:
        user.last_reset_date = now
        db.commit()

    current_usage = user.files_used_today if user.plan == UserPlan.free else user.files_used_month

    if current_usage >= limits["files"]:
        upgrade_map = {
            UserPlan.free: "starter",
            UserPlan.starter: "business",
            UserPlan.business: "enterprise",
        }
        next_plan = upgrade_map.get(user.plan, "enterprise")

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": f"Plan limit reached. Your {user.plan.value} plan allows {limits['files']} files.",
                "current_usage": current_usage,
                "limit": limits["files"],
                "action": f"Upgrade to {next_plan} plan for more extractions.",
                "upgrade_url": "/pricing",
            },
        )

    # Increment usage
    user.files_used_today = (user.files_used_today or 0) + 1
    user.files_used_month = (user.files_used_month or 0) + 1
    db.commit()


# ─────────────────────────────────────────────────────────────
#  Combined check — call this on every upload endpoint
#  Handles both anonymous and logged-in users automatically
# ─────────────────────────────────────────────────────────────


def enforce_rate_limit(
    request: Request,
    db: Session,
    current_user: User | None = None,
) -> str:
    """
    Single function to call on every upload.
    - If user logged in  → check plan limit
    - If user anonymous  → check IP limit
    Returns the IP address (useful for tagging anonymous documents).
    """
    ip = get_client_ip(request)

    if current_user:
        check_user_plan_limit(current_user, db)
    else:
        check_ip_rate_limit(ip, db)

    return ip
