"""Pydantic DTOs for the /api/auth/* endpoints.

Kept deliberately thin: only the HTTP boundary shapes live here. Domain
enforcement (lockout, email-uniqueness, token consumption) stays in the
service layer so we can reuse it when OAuth providers land in phase 2.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Username: 3-32 chars, alnum + underscore + dash. Matches what most SaaS
# products allow and plays nicely with URL path parameters.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")


def _validate_password_strength(password: str, min_length: int = 10) -> str:
    """Server-side strength check mirroring requirements §4.4.

    We intentionally don't require special characters — the research users
    this is aimed at find that annoying and it doesn't meaningfully add
    entropy for the 10-char minimum we already enforce.
    """
    if len(password) < min_length:
        raise ValueError(
            f"password must be at least {min_length} characters long"
        )
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_letter and has_digit):
        raise ValueError("password must contain at least 1 letter and 1 digit")
    return password


class RegisterIn(BaseModel):
    """Body for ``POST /api/auth/register``."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., description="3-32 alnum/_- chars")
    email: EmailStr
    password: str = Field(..., min_length=10, max_length=256)
    invite_code: str | None = None

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError(
                "username must be 3-32 chars, alnum / underscore / dash only"
            )
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username_or_email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class EmailVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=8)


class PasswordResetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=8)
    new_password: str = Field(..., min_length=10, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class ChangePasswordIn(BaseModel):
    """Body for ``POST /api/auth/change-password`` (Settings > Password)."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=10, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _check_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class ChangeEmailIn(BaseModel):
    """Body for ``POST /api/auth/change-email`` (Settings > Profile).

    Reuses the existing ``EmailVerification`` table with a new
    ``kind='email_change'`` discriminator. The new email is held in the
    token row's ``payload`` column until the user clicks the confirmation
    link mailed to ``new_email`` — only then does the User row update.
    """

    model_config = ConfigDict(extra="forbid")

    new_email: EmailStr
    current_password: str = Field(..., min_length=1, max_length=256)


class UserOut(BaseModel):
    """Public user projection — never leaks password hash or lock state.

    ``github_login`` and ``has_password`` are surfaced so the settings
    UI can render the right "link / unlink / set-password" affordances
    without a second round-trip. ``has_password`` is derived from
    ``password_hash IS NOT NULL`` at serialisation time.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    is_admin: bool
    email_verified: bool
    is_active: bool
    created_at: str
    last_login: str | None = None
    github_login: str | None = None
    has_password: bool = False
    # Deprecated: demo now unconditionally hidden from logged-in users
    # since 2026-04-24. Field retained so older frontends that read it
    # keep parsing responses; the stored value no longer affects any
    # visibility rule.
    hide_demo: bool = False

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[override]
        """Inject ``has_password`` from the User's ``password_hash`` column."""
        if hasattr(obj, "password_hash") and not isinstance(obj, dict):
            data = {
                "id": obj.id,
                "username": obj.username,
                "email": obj.email,
                "is_admin": obj.is_admin,
                "email_verified": obj.email_verified,
                "is_active": obj.is_active,
                "created_at": obj.created_at,
                "last_login": obj.last_login,
                "github_login": getattr(obj, "github_login", None),
                "has_password": bool(getattr(obj, "password_hash", None)),
                "hide_demo": bool(getattr(obj, "hide_demo", False)),
            }
            return super().model_validate(data, *args, **kwargs)
        return super().model_validate(obj, *args, **kwargs)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until ``access_token`` expires
    user: UserOut


class RegisterOut(BaseModel):
    user_id: int
    require_verify: bool = True


class OkResponse(BaseModel):
    ok: bool = True
    detail: str | None = None


class ActiveSessionOut(BaseModel):
    """One row in ``GET /api/auth/sessions`` (Settings > Sessions panel).

    Mirrors :class:`backend.models.ActiveSession` minus the user_id (the
    caller already knows that, since the endpoint scopes to the caller).
    ``is_current=True`` tags the JWT used to make *this* request so the
    UI can render "logout all others" vs "logout this one".
    """

    model_config = ConfigDict(from_attributes=True)

    jti: str
    issued_at: str
    expires_at: str
    user_agent: str | None = None
    ip: str | None = None
    last_seen_at: str | None = None
    is_current: bool = False
