"""Accounts and sessions — signup, login, refresh rotation, logout.

Refresh tokens are *rotated*: spending one revokes it and issues a new pair. A
token that comes back a second time is therefore either a replay or a stolen
copy racing the real client, and neither can be told apart from the other — so
presenting an already-revoked token drops every session that user has, which
locks out the thief at the cost of one re-login for the owner.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from core.security import (
    AuthError,
    access_lifetime,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from models.schemas import TokenPair, UserOut
from models.user import RefreshToken, User, utcnow

logger = logging.getLogger(__name__)


class EmailTaken(AuthError):
    """The address is already registered.

    Split out from the other AuthErrors because it is the one that is not a
    failed credential: the caller is told which of their details to change, and
    the route answers 409 rather than 401 or 422.
    """


# Compared against when the email is unknown, so a login for a non-existent
# account costs the same time as one with the wrong password — otherwise the
# response time alone tells an attacker which addresses are registered.
_DUMMY_HASH = "$2b$12$" + "." * 53


class AuthService:
    """Everything the API needs to open, verify and end a user's session."""

    async def signup(
        self,
        session: AsyncSession,
        email: str,
        name: str,
        password: str,
    ) -> TokenPair:
        """Open an account and log it straight in.

        Raises:
            AuthError: if the address is already registered, or the details do
                not satisfy the rules on ``UserBase``.
        """
        email = _normalize(email)

        # model_validate() rather than User(...): SQLModel skips validation
        # when a table class is constructed directly, so this is what makes the
        # UserBase rules hold for the row itself and not only for the request
        # body that usually precedes it. The API validates before calling here;
        # this is what keeps that true for any other caller.
        try:
            user = User.model_validate(
                {
                    "email": email,
                    "name": name,
                    "password_hash": hash_password(password),
                }
            )
        except ValidationError as error:
            raise AuthError(_first_problem(error)) from error

        session.add(user)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            # Decided by the unique index on User.email, not by a prior SELECT,
            # so two simultaneous signups cannot both win.
            raise EmailTaken(
                "An account with that email already exists."
            ) from error

        pair = await self._issue(session, user)
        await session.commit()
        logger.info("Account created: %s (%s)", user.email, user.id)
        return pair

    async def login(
        self,
        session: AsyncSession,
        email: str,
        password: str,
    ) -> TokenPair:
        """Exchange credentials for a token pair.

        Raises:
            AuthError: on a wrong password, an unknown address or a disabled
                account — all with the same message, so the response never
                reveals which addresses are registered.
        """
        user = await self._by_email(session, _normalize(email))

        if user is None:
            verify_password(password, _DUMMY_HASH)
            raise AuthError("Email or password is incorrect.")
        if not verify_password(password, user.password_hash):
            logger.info("Failed login for %s", user.email)
            raise AuthError("Email or password is incorrect.")
        if not user.is_active:
            raise AuthError("This account has been disabled.")

        pair = await self._issue(session, user)
        await session.commit()
        logger.info("Login: %s (%s)", user.email, user.id)
        return pair

    async def refresh(self, session: AsyncSession, refresh_token: str) -> TokenPair:
        """Spend a refresh token for a new pair, revoking the one presented.

        Raises:
            AuthError: if the token is invalid, expired, already spent, or
                belongs to an account that is gone or disabled.
        """
        claims = decode_token(refresh_token, "refresh")
        jti = claims.get("jti", "")
        user_id = str(claims["sub"])

        record = (
            await session.exec(select(RefreshToken).where(RefreshToken.jti == jti))
        ).first()

        if record is None:
            # Signed by us, but no row: the session was ended, or the row was
            # cleaned out after expiry. Either way it buys nothing.
            raise AuthError("This session has ended. Please sign in again.")

        if not record.is_usable:
            # Already spent or explicitly revoked — see the module docstring.
            await self.revoke_all(session, record.user_id)
            await session.commit()
            logger.warning(
                "Reused refresh token for user %s — all sessions revoked",
                record.user_id,
            )
            raise AuthError("This session has ended. Please sign in again.")

        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            raise AuthError("This account is no longer active.")

        record.revoked_at = utcnow()
        pair = await self._issue(session, user)
        await session.commit()
        logger.debug("Rotated refresh token for %s", user.id)
        return pair

    async def logout(self, session: AsyncSession, refresh_token: str) -> None:
        """End one session. Silent when the token is already dead.

        Logging out is not a place to report problems: whatever the client
        holds is unusable afterwards either way.
        """
        try:
            claims = decode_token(refresh_token, "refresh")
        except AuthError:
            return

        await session.exec(
            update(RefreshToken)
            .where(
                RefreshToken.jti == claims.get("jti", ""),
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        await session.commit()

    async def revoke_all(self, session: AsyncSession, user_id: str) -> None:
        """Revoke every live refresh token a user holds. Does not commit."""
        await session.exec(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )

    async def user_from_access_token(
        self,
        session: AsyncSession,
        token: str,
    ) -> User:
        """Resolve a bearer token to its user.

        Access tokens are not looked up in the database — that is the point of
        them — so revoking a session takes effect on the access token's own
        (short) expiry. Only the account's continued existence is checked.

        Raises:
            AuthError: if the token is invalid, expired, or its user is gone.
        """
        claims = decode_token(token, "access")
        user = await session.get(User, str(claims["sub"]))
        if user is None or not user.is_active:
            raise AuthError("This account is no longer active.")
        return user

    async def _issue(self, session: AsyncSession, user: User) -> TokenPair:
        """Mint a pair and record the refresh token's jti. Does not commit."""
        refresh_token, jti, expires_at = create_refresh_token(user.id)
        session.add(
            RefreshToken(jti=jti, user_id=user.id, expires_at=expires_at)
        )
        return TokenPair(
            access_token=create_access_token(user.id),
            refresh_token=refresh_token,
            expires_in=int(access_lifetime().total_seconds()),
            user=UserOut.model_validate(user),
        )

    async def _by_email(self, session: AsyncSession, email: str) -> User | None:
        return (await session.exec(select(User).where(User.email == email))).first()


def _normalize(email: str) -> str:
    """Emails are matched case-insensitively, so they are stored folded."""
    return email.strip().lower()


def _first_problem(error: ValidationError) -> str:
    """The first rejected field, as a sentence. The rest follow from it."""
    first = error.errors()[0]
    field = first["loc"][-1] if first["loc"] else "input"
    return f"{field}: {first['msg']}"
