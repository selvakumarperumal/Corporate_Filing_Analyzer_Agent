"""Request and response bodies for the auth endpoints.

These are the *validating* half of the SQLModel pair: unlike the table classes
in :mod:`auth.models`, nothing here sets ``table=True``, so every field rule
actually runs. Inheriting ``UserBase`` is what keeps the email and name rules
identical between what a request may contain and what a row may hold.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr
from sqlmodel import Field, SQLModel

from auth.models import UserBase


class SignupRequest(UserBase):
    """What opening an account requires: the analyst's details, and a password.

    Length is the requirement that actually buys resistance to guessing;
    composition rules mostly buy predictable passwords.
    """

    password: str = Field(min_length=8, max_length=128)


class LoginRequest(SQLModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(SQLModel):
    refresh_token: str


class UserOut(UserBase):
    """The analyst as the API reports them — never the password hash."""

    id: str
    created_at: datetime


class TokenPair(SQLModel):
    """A fresh access/refresh pair, plus who it belongs to.

    ``expires_in`` is the access token's lifetime in seconds, so the client can
    refresh ahead of expiry instead of waiting for a 401.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
