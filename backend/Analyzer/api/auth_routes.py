"""Auth endpoints — signup, login, refresh, logout, and who am I.

    POST /api/auth/signup   open an account, logged straight in
    POST /api/auth/login    exchange credentials for a token pair
    POST /api/auth/refresh  spend a refresh token for a new pair
    POST /api/auth/logout   end this session
    GET  /api/auth/me       the signed-in analyst
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from api.deps import Auth, CurrentUser, DbSession, unauthorized
from core.security import AuthError
from models.schemas import (
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
    UserOut,
)
from services.auth_service import EmailTaken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupRequest, session: DbSession, auth: Auth) -> TokenPair:
    """Open an account. The address must not already be registered."""
    try:
        return await auth.signup(
            session, email=body.email, name=body.name, password=body.password
        )
    except EmailTaken as error:
        # Not a failed credential — a detail the caller can change and retry.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except AuthError as error:
        # The row itself failed validation. The request body is checked before
        # this point, so reaching here means a rule the schema does not cover
        # (an over-long bcrypt password, say) — still the caller's to fix.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, session: DbSession, auth: Auth) -> TokenPair:
    """Sign in and receive an access/refresh pair."""
    try:
        return await auth.login(
            session, email=body.email, password=body.password
        )
    except AuthError as error:
        raise unauthorized(str(error)) from error


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, session: DbSession, auth: Auth) -> TokenPair:
    """Trade a refresh token for a new pair; the old one stops working."""
    try:
        return await auth.refresh(session, body.refresh_token)
    except AuthError as error:
        raise unauthorized(str(error)) from error


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(body: RefreshRequest, session: DbSession, auth: Auth) -> dict[str, str]:
    """End this session. Always succeeds — the token is unusable either way."""
    await auth.logout(session, body.refresh_token)
    return {"status": "ok"}


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    """The signed-in analyst, for a client restoring a stored session."""
    return UserOut.model_validate(user)
