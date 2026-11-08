"""Hashing, tokens and rate limiting. No business rules here."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

import jwt
from fastapi import HTTPException, Request

ALGORITHM = "HS256"


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ── OTP: 6 digits, stored only as a keyed hash (HMAC with a key derived from JWT_SECRET)
def generate_otp(digits: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(digits))


def hash_otp(code: str, salt: str, secret: str) -> str:
    key = hashlib.sha256(b"otp:" + secret.encode()).digest()
    return hmac.new(key, f"{salt}:{code}".encode(), hashlib.sha256).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ── Refresh tokens: random, opaque, stored only as SHA-256
def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Access tokens: short-lived JWT. Role is NOT embedded; it is read from the user record
# on every request so role changes and deactivation apply immediately.
def create_access_token(user_id: str, session_id: str, secret: str, minutes: int, now: datetime) -> str:
    payload = {"sub": user_id, "sid": session_id, "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict | None:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub", "sid"]})
    except jwt.PyJWTError:
        return None


# ── Rate limiting: per client IP, in memory (single Render instance, free tier)
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(name: str, limit: int, window_seconds: int = 60):
    """FastAPI dependency factory: `Depends(rate_limit("otp_request", 20))`."""

    async def dependency(request: Request) -> None:
        limiter: RateLimiter = request.app.state.rate_limiter
        if not limiter.check(f"{name}:{client_ip(request)}", limit, window_seconds):
            raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment.")

    return dependency
