"""Test fixtures. MongoDB is simulated in memory (mongomock-motor); emails are captured, not sent."""

import re
from datetime import timedelta

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.core.db import utcnow
from app.core.mailer import OutboundEmail
from app.main import create_app

API = "/api/v1"


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_email(self, email: OutboundEmail) -> None:
        self.sent.append(
            {
                "to": email.to,
                "subject": email.subject,
                "body": email.text or email.html or "",
                "html": email.html or "",
                "attachments": [
                    {"filename": item.filename, "content_type": item.content_type, "content": item.content}
                    for item in email.attachments
                ],
            }
        )

    async def send_email_in_background(self, email: OutboundEmail) -> None:
        await self.send_email(email)

    def last_code(self, email: str) -> str:
        mails = [m for m in self.sent if m["to"] == email]
        return re.search(r"\b(\d{6})\b", mails[-1]["body"]).group(1)


class Portal:
    """Bundles the app, database, mailer and an HTTP client for one test."""

    def __init__(self, app, db, mailer, client):
        self.app, self.db, self.mailer, self.client = app, db, mailer, client

    async def add_user(self, email: str, role: str = "staff", active: bool = True) -> str:
        res = await self.db["users"].insert_one(
            {
                "email": email,
                "name": email.split("@")[0],
                "role": role,
                "active": active,
                "invited_by": None,
                "created_at": utcnow(),
                "last_login_at": None,
            }
        )
        return str(res.inserted_id)

    async def login(self, email: str, client: httpx.AsyncClient | None = None) -> str:
        """Full OTP flow; returns the access token."""
        c = client or self.client
        r = await c.post(f"{API}/auth/otp/request", json={"email": email})
        assert r.status_code == 202, r.text
        r = await c.post(f"{API}/auth/otp/verify", json={"email": email, "code": self.mailer.last_code(email)})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    @staticmethod
    def auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    async def as_user(self, email: str, role: str = "staff") -> dict:
        await self.add_user(email, role)
        return self.auth(await self.login(email))

    def set_cooldown(self, seconds: int) -> None:
        self.app.state.config.app.auth.otp.resend_cooldown_seconds = seconds

    def build_modules(self, *keys: str) -> None:
        """Pretend these modules are built (their backend folder exists), so tiles appear."""
        self.app.state.mounted_modules |= set(keys)

    async def age_challenges(self, email: str, seconds: int) -> None:
        """Move OTP challenge timestamps into the past (simulate time passing)."""
        delta = timedelta(seconds=seconds)
        async for c in self.db["otp_challenges"].find({"email": email}):
            await self.db["otp_challenges"].update_one(
                {"_id": c["_id"]},
                {"$set": {"created_at": c["created_at"] - delta, "expires_at": c["expires_at"] - delta}},
            )


@pytest.fixture
async def portal():
    settings = Settings(
        _env_file=None,
        environment="test",
        mongodb_uri="mongodb://unused",
        jwt_secret="t" * 40,
        bootstrap_admin_email="boss@example.com",
    )
    app = create_app(settings)
    db = AsyncMongoMockClient()["portal_test"]
    app.state.db = db
    mailer = FakeMailer()
    app.state.mailer = mailer
    app.state.config.app.auth.otp.resend_cooldown_seconds = 0  # tests that need it set it back
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield Portal(app, db, mailer, client)
