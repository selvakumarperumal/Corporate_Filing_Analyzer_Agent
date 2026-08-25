"""Accounts and sessions.

Everything about *who is asking* lives here — the two tables, the password and
JWT primitives, the service that opens and ends a session, and the routes that
expose it. Nothing in this package knows what a filing or a dossier is.
"""

from auth.models import RefreshToken, User, UserBase
from auth.schemas import (
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
    UserOut,
)
from auth.security import AuthError
from auth.service import AuthService, EmailTaken

__all__ = [
    "AuthError",
    "AuthService",
    "EmailTaken",
    "LoginRequest",
    "RefreshRequest",
    "RefreshToken",
    "SignupRequest",
    "TokenPair",
    "User",
    "UserBase",
    "UserOut",
]
