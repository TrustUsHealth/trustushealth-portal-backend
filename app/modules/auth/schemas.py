from pydantic import BaseModel, EmailStr, Field

from app.core.config import Role


class OtpRequestIn(BaseModel):
    email: EmailStr


class OtpRequestedOut(BaseModel):
    """Identical whether or not the email is registered (no account enumeration)."""

    message: str = "If this email is registered, a login code has been sent."
    resend_after_seconds: int


class OtpVerifyIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=8, pattern=r"^\d+$")


class UserOut(BaseModel):
    id: str
    email: str
    name: str | None = None
    role: Role


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: UserOut
