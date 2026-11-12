"""Shared FastAPI dependencies: app services, current user, role checks."""

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import PortalConfig, Role, Settings
from app.core.mailer import Mailer
from app.core.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    id: str
    email: str
    name: str | None = None
    role: Role


def get_db(request: Request) -> AsyncDatabase:
    return request.app.state.db


def get_config(request: Request) -> PortalConfig:
    return request.app.state.config


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_mailer(request: Request) -> Mailer:
    return request.app.state.mailer


async def get_current_user(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> CurrentUser:
    unauthorized = HTTPException(status_code=401, detail="Not signed in", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    claims = decode_access_token(creds.credentials, request.app.state.settings.jwt_secret)
    if claims is None:
        raise unauthorized
    db: AsyncDatabase = request.app.state.db
    try:
        user = await db["users"].find_one({"_id": ObjectId(claims["sub"])})
    except InvalidId:
        raise unauthorized from None
    if not user or not user.get("active"):
        raise unauthorized
    # Logout / reuse detection revokes the whole session immediately, not after 15 minutes.
    if await db["refresh_tokens"].find_one({"family_id": claims["sid"], "revoked": True}):
        raise unauthorized
    return CurrentUser(id=str(user["_id"]), email=user["email"], name=user.get("name"), role=user["role"])


def require_role(*roles: Role):
    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission for this.")
        return user

    return dependency
