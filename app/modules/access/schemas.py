from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.core.config import Role


class SubmoduleOut(BaseModel):
    key: str
    name: str
    kind: Literal["submodule", "lab"]


class TileOut(BaseModel):
    """One dashboard tile. State comes from the server; the frontend never decides who sees what."""

    key: str
    name: str
    icon: str
    route: str
    state: Literal["open", "locked", "pending"]  # locked = Request Access, pending = Request pending
    submodules: list[SubmoduleOut] = []  # only the ones this user may open
    visible_to_staff: bool | None = None  # Admin view only


class ModuleAdminOut(BaseModel):
    key: str
    name: str
    icon: str
    route: str
    visible_to_staff: bool
    staff_eligible: bool  # false = Admin-only module, toggle not offered
    submodules: list[SubmoduleOut]


class VisibilityIn(BaseModel):
    visible_to_staff: bool


class UserAdminOut(BaseModel):
    id: str
    email: str
    name: str | None
    role: Role
    active: bool
    created_at: datetime
    last_login_at: datetime | None


class InviteIn(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=100)
    role: Literal["admin", "staff", "engineer"]


class UserPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    role: Literal["admin", "staff", "engineer"] | None = None
    active: bool | None = None


class AccessRequestIn(BaseModel):
    module: str
    submodule: str | None = None


class GrantIn(BaseModel):
    user_id: str
    module: str
    submodule: str | None = None


class GrantOut(BaseModel):
    id: str
    user_id: str
    user_email: str | None = None
    module: str
    submodule: str | None
    status: Literal["requested", "granted"]
    requested_at: datetime | None = None
    granted_at: datetime | None = None


class MatrixColumn(BaseModel):
    module: str
    module_name: str
    submodule: str | None
    label: str


class MatrixRow(BaseModel):
    user: UserAdminOut
    grants: list[GrantOut]


class MatrixOut(BaseModel):
    columns: list[MatrixColumn]
    rows: list[MatrixRow]


class PartnerOut(BaseModel):
    key: str
    name: str


class AppConfigOut(BaseModel):
    """Non-secret settings the frontend needs (brand, timezone, ...). Public: used by the login page."""

    brand_name: str
    logo_full: str
    logo_mark: str
    favicon: str
    timezone: str
    currency: str
    upload_max_mb: int
    upload_allowed: list[str]
