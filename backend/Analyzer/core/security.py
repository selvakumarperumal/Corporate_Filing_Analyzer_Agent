"""Password hashing and JWT minting/verification.

Two token kinds, both signed with ``JWT_SECRET_KEY`` and distinguished by a
``type`` claim that is checked on the way back in:

    access   short-lived (minutes), sent on every request, never stored
    refresh  long-lived (days), spent once to get a new pair, revocable by jti

The ``type`` check is the point: an access token must not be usable at the
refresh endpoint, and a refresh token must not authenticate a request.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from core.config import settings

logger = logging.getLogger(__name__)

TokenType = Literal["access", "refresh"]

# bcrypt hashes at most 72 bytes and silently ignores the rest, which would
# make two long passwords sharing a prefix interchangeable. Rejected instead.
_MAX_PASSWORD_BYTES = 72


class AuthError(Exception):
    """A credential or token was not acceptable. Surfaces as a 401."""


def _secret() -> str:
    """The signing key, generating a throwaway one only for local runs.

    A process that restarts with a new random key invalidates every token it
    ever issued — fine while developing, which is why it is loud, and why
    anything real must set JWT_SECRET_KEY.
    """
    if settings.JWT_SECRET_KEY:
        return settings.JWT_SECRET_KEY
    global _EPHEMERAL_SECRET
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(64)
        logger.warning(
            "JWT_SECRET_KEY is not set — signing with a random key that dies "
            "with this process. Every login is invalidated on restart. Set "
            "JWT_SECRET_KEY in .env before deploying."
        )
    return _EPHEMERAL_SECRET


_EPHEMERAL_SECRET: str | None = None


# ── Passwords ────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Hash a password for storage. Raises AuthError if it is too long."""
    encoded = password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise AuthError("Password is too long (max 72 bytes).")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash, never raising on bad input."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:_MAX_PASSWORD_BYTES],
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # A malformed hash in the row is a failed login, not a 500.
        return False


# ── Tokens ───────────────────────────────────────────────────────────────
def create_access_token(user_id: str) -> str:
    """Mint a short-lived token that authenticates requests."""
    token, _, _ = _encode("access", user_id, access_lifetime())
    return token


def create_refresh_token(user_id: str) -> tuple[str, str, datetime]:
    """Mint a refresh token. Returns ``(token, jti, expires_at)``.

    The caller records the jti so the token can later be revoked; a refresh
    token with no live row is refused however well it is signed.
    """
    return _encode("refresh", user_id, refresh_lifetime())


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Verify a token's signature, expiry and kind, returning its claims.

    Raises:
        AuthError: if it is malformed, expired, tampered with, or the wrong
            kind for where it was presented.
    """
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as error:
        raise AuthError(f"This {expected_type} token has expired.") from error
    except jwt.InvalidTokenError as error:
        raise AuthError("Token is not valid.") from error

    if claims.get("type") != expected_type:
        # e.g. a refresh token presented as a bearer credential.
        article = "an" if expected_type == "access" else "a"
        raise AuthError(f"That is not {article} {expected_type} token.")
    if not claims.get("sub"):
        raise AuthError("Token names no user.")
    return claims


def access_lifetime() -> timedelta:
    return timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)


def refresh_lifetime() -> timedelta:
    return timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def _encode(
    token_type: TokenType,
    user_id: str,
    lifetime: timedelta,
) -> tuple[str, str, datetime]:
    """Sign one token. Returns ``(token, jti, expires_at)``."""
    issued_at = datetime.now(UTC)
    expires_at = issued_at + lifetime
    jti = uuid.uuid4().hex
    token = jwt.encode(
        {
            "sub": user_id,
            "type": token_type,
            "jti": jti,
            "iat": issued_at,
            "exp": expires_at,
        },
        _secret(),
        algorithm=settings.JWT_ALGORITHM,
    )
    return token, jti, expires_at
