"""Append-only audit log (PRD 10.5). Every module calls `record()`; nothing here edits or deletes.

Rules: store the NAMES of changed fields, never patient values; never store OTP codes or tokens.
Records expire after 365 days (TTL index).
"""

import logging
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Request
from pymongo.asynchronous.database import AsyncDatabase

from app.core.db import utcnow
from app.core.deps import CurrentUser
from app.core.security import client_ip

log = logging.getLogger(__name__)
COLLECTION = "audit_logs"
Outcome = Literal["success", "failure", "denied"]


async def ensure_indexes(db: AsyncDatabase) -> None:
    c = db[COLLECTION]
    await c.create_index("ts", expireAfterSeconds=365 * 24 * 3600)
    await c.create_index([("actor_id", 1), ("ts", -1)])
    await c.create_index([("module", 1), ("action", 1), ("ts", -1)])


async def record(
    request: Request,
    action: str,
    *,
    actor: CurrentUser | None = None,  # None for anonymous events (e.g. failed login)
    actor_email: str | None = None,
    module: str | None = None,
    submodule: str | None = None,
    target: str | None = None,
    fields_changed: list[str] | None = None,
    outcome: Outcome = "success",
) -> None:
    """Write one audit record. Never raises: a logging fault must not break the request."""
    try:
        tz = ZoneInfo(request.app.state.config.app.timezone)
        now: datetime = utcnow()
        doc = {
            "ts": now,
            "ts_local": now.astimezone(tz).isoformat(timespec="seconds"),  # IST by default
            "actor_id": getattr(actor, "id", None),
            "actor_email": getattr(actor, "email", None) or actor_email,
            "role": getattr(actor, "role", None),
            "action": action,
            "module": module,
            "submodule": submodule,
            "target": target,
            "ip": client_ip(request),
            "device": (request.headers.get("user-agent") or "")[:200],
            "outcome": outcome,
            "fields_changed": fields_changed or [],
        }
        await request.app.state.db[COLLECTION].insert_one(doc)
    except Exception:  # noqa: BLE001
        log.exception("audit write failed for action=%s", action)
