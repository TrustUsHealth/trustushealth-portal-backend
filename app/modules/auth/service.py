"""Email-OTP login and session tokens.

OTP : 6 digits, stored hashed, 5 min expiry, 5 attempts, 60 s resend cooldown, 5 requests/hour.
Session: 15 min access token + rotating refresh token, 12 h hard maximum, logout revokes.
Refresh tokens rotate on every use; presenting an already-used token revokes the whole session
(theft detection), except within a short grace window to tolerate two tabs refreshing at once.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import LOGIN_ROLES, PortalConfig, Settings
from app.core.db import as_utc, utcnow
from app.core.security import (
    create_access_token,
    generate_otp,
    hash_otp,
    hash_token,
    new_refresh_token,
    normalize_email,
    safe_equal,
)

USERS, OTP, REFRESH = "users", "otp_challenges", "refresh_tokens"
REUSE_GRACE_SECONDS = 10


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db[USERS].create_index("email", unique=True)
    await db[OTP].create_index([("email", 1), ("created_at", -1)])
    await db[OTP].create_index("purge_at", expireAfterSeconds=0)  # TTL
    await db[REFRESH].create_index("token_hash", unique=True)
    await db[REFRESH].create_index("family_id")
    await db[REFRESH].create_index("purge_at", expireAfterSeconds=0)  # TTL


async def on_startup(app) -> None:
    await ensure_indexes(app.state.db)
    await bootstrap_admin(app.state.db, app.state.settings.bootstrap_admin_email)


async def bootstrap_admin(db: AsyncDatabase, email: str | None) -> None:
    """Create the first Admin from BOOTSTRAP_ADMIN_EMAIL if that email has no account yet."""
    if not email:
        return
    try:
        await db[USERS].insert_one(
            {
                "email": normalize_email(email),
                "name": None,
                "role": "admin",
                "active": True,
                "invited_by": None,
                "created_at": utcnow(),
                "last_login_at": None,
            }
        )
    except DuplicateKeyError:
        pass


# ───────────────────────────── OTP ─────────────────────────────
@dataclass
class OtpDecision:
    code: str | None  # set only when an email should be sent
    skipped: str | None = None  # audit reason: unknown_email | cooldown | hourly_limit


async def request_otp(db: AsyncDatabase, cfg: PortalConfig, settings: Settings, email: str) -> OtpDecision:
    otp_cfg = cfg.app.auth.otp
    email = normalize_email(email)
    now = utcnow()

    user = await db[USERS].find_one({"email": email, "active": True, "role": {"$in": list(LOGIN_ROLES)}})
    if not user:
        return OtpDecision(None, "unknown_email")

    latest = await db[OTP].find({"email": email}).sort("created_at", -1).limit(1).to_list(1)
    if latest and (now - as_utc(latest[0]["created_at"])).total_seconds() < otp_cfg.resend_cooldown_seconds:
        return OtpDecision(None, "cooldown")
    recent = await db[OTP].count_documents({"email": email, "created_at": {"$gt": now - timedelta(hours=1)}})
    if recent >= otp_cfg.max_requests_per_hour:
        return OtpDecision(None, "hourly_limit")

    await db[OTP].update_many({"email": email, "consumed": False}, {"$set": {"consumed": True}})
    code, salt = generate_otp(otp_cfg.digits), secrets.token_hex(8)
    await db[OTP].insert_one(
        {
            "email": email,
            "salt": salt,
            "code_hash": hash_otp(code, salt, settings.jwt_secret),
            "attempts": 0,
            "max_attempts": otp_cfg.max_attempts,
            "consumed": False,
            "created_at": now,
            "expires_at": now + timedelta(minutes=otp_cfg.expires_minutes),
            "purge_at": now + timedelta(hours=2),  # kept 2 h so the hourly limit can count them
        }
    )
    return OtpDecision(code)


async def verify_otp(db: AsyncDatabase, settings: Settings, email: str, code: str) -> dict | None:
    """Return the user document if the code is valid (single use), else None."""
    email, now = normalize_email(email), utcnow()
    challenge = await db[OTP].find_one(
        {"email": email, "consumed": False, "expires_at": {"$gt": now}}, sort=[("created_at", -1)]
    )
    if not challenge:
        return None
    # Atomic attempt counter: concurrent guesses cannot exceed the limit.
    challenge = await db[OTP].find_one_and_update(
        {"_id": challenge["_id"], "consumed": False, "attempts": {"$lt": challenge["max_attempts"]}},
        {"$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if challenge is None:
        return None
    if not safe_equal(hash_otp(code.strip(), challenge["salt"], settings.jwt_secret), challenge["code_hash"]):
        if challenge["attempts"] >= challenge["max_attempts"]:
            await db[OTP].update_one({"_id": challenge["_id"]}, {"$set": {"consumed": True}})
        return None
    used = await db[OTP].find_one_and_update({"_id": challenge["_id"], "consumed": False}, {"$set": {"consumed": True}})
    if used is None:  # lost a race with a parallel verify of the same code
        return None
    user = await db[USERS].find_one({"email": email, "active": True, "role": {"$in": list(LOGIN_ROLES)}})
    if user:
        await db[USERS].update_one({"_id": user["_id"]}, {"$set": {"last_login_at": now}})
    return user


# ───────────────────────────── sessions ─────────────────────────────
@dataclass
class Session:
    access_token: str
    refresh_token: str
    refresh_expires_at: datetime
    expires_in: int
    user: dict


async def _store_refresh(
    db: AsyncDatabase, cfg: PortalConfig, user_id: str, family: str, started: datetime, now: datetime
) -> tuple[str, datetime]:
    token = new_refresh_token()
    expires = started + timedelta(hours=cfg.app.session.max_session_hours)  # hard cap
    await db[REFRESH].insert_one(
        {
            "family_id": family,
            "user_id": user_id,
            "token_hash": hash_token(token),
            "session_started_at": started,
            "issued_at": now,
            "expires_at": expires,
            "used": False,
            "revoked": False,
            "purge_at": expires + timedelta(days=1),
        }
    )
    return token, expires


def _access(cfg: PortalConfig, settings: Settings, user: dict, family: str, now: datetime) -> tuple[str, int]:
    minutes = cfg.app.session.access_token_minutes
    token = create_access_token(str(user["_id"]), family, settings.jwt_secret, minutes, now)
    return token, minutes * 60


async def start_session(db: AsyncDatabase, cfg: PortalConfig, settings: Settings, user: dict) -> Session:
    now, family = utcnow(), secrets.token_hex(16)
    refresh, expires = await _store_refresh(db, cfg, str(user["_id"]), family, now, now)
    access, ttl = _access(cfg, settings, user, family, now)
    return Session(access, refresh, expires, ttl, user)


@dataclass
class RefreshResult:
    session: Session | None = None
    reuse_detected: bool = False
    user_id: str | None = None


async def revoke_family(db: AsyncDatabase, family_id: str) -> None:
    await db[REFRESH].update_many({"family_id": family_id}, {"$set": {"revoked": True}})


async def rotate_refresh(db: AsyncDatabase, cfg: PortalConfig, settings: Settings, token: str) -> RefreshResult:
    now, token_hash = utcnow(), hash_token(token)
    # Atomic "use once": only one concurrent request can consume a given token.
    doc = await db[REFRESH].find_one_and_update(
        {"token_hash": token_hash, "used": False, "revoked": False, "expires_at": {"$gt": now}},
        {"$set": {"used": True, "used_at": now}},
    )
    if doc is None:
        old = await db[REFRESH].find_one({"token_hash": token_hash})
        if old and old.get("used") and not old.get("revoked"):
            if (now - as_utc(old["used_at"])).total_seconds() > REUSE_GRACE_SECONDS:
                await revoke_family(db, old["family_id"])
                return RefreshResult(reuse_detected=True, user_id=old["user_id"])
        return RefreshResult()
    user = await db[USERS].find_one({"_id": ObjectId(doc["user_id"]), "active": True})
    if not user:
        await revoke_family(db, doc["family_id"])
        return RefreshResult()
    new_refresh, expires = await _store_refresh(
        db, cfg, doc["user_id"], doc["family_id"], as_utc(doc["session_started_at"]), now
    )
    access, ttl = _access(cfg, settings, user, doc["family_id"], now)
    return RefreshResult(Session(access, new_refresh, expires, ttl, user), user_id=doc["user_id"])


async def logout(db: AsyncDatabase, token: str | None) -> dict | None:
    """Revoke the whole session for this refresh token. Returns the user document, if known."""
    if not token:
        return None
    doc = await db[REFRESH].find_one({"token_hash": hash_token(token)})
    if not doc:
        return None
    await revoke_family(db, doc["family_id"])
    return await db[USERS].find_one({"_id": ObjectId(doc["user_id"])})
