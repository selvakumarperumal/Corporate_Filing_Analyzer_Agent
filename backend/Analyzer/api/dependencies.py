"""How a route asks for a database session, a service, or the current user.

Plumbing only: what these hand back is built in :mod:`container`. The seam is
the point — a route that receives its services through ``Depends`` can have
them swapped in a test with ``app.dependency_overrides``, which a module-level
import cannot.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel.ext.asyncio.session import AsyncSession

from analysis.pipeline import AnalysisPipeline
from auth.models import User
from auth.security import AuthError
from auth.service import AuthService
from container import analysis_pipeline, auth_service, history_service
from conversations.service import HistoryService
from db.engine import get_session

# auto_error=False so a missing header comes back as our own 401 with a
# WWW-Authenticate hint, rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


def get_auth_service() -> AuthService:
    """The account service: signup, login, refresh, token verification."""
    return auth_service


def get_analysis_pipeline() -> AnalysisPipeline:
    """The analysis pipeline and the vector store behind it."""
    return analysis_pipeline


def get_history_service() -> HistoryService:
    """The conversation ledger — messages, filings, rolling summaries."""
    return history_service


Auth = Annotated[AuthService, Depends(get_auth_service)]
Analysis = Annotated[AnalysisPipeline, Depends(get_analysis_pipeline)]
History = Annotated[HistoryService, Depends(get_history_service)]


def unauthorized(detail: str) -> HTTPException:
    """A 401 that tells the client a bearer token is what was wanted."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    session: DbSession,
    auth: Auth,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolve the bearer access token to its user, or refuse the request.

    Every 401 out of here means "get a new access token" — the client answers
    it by spending its refresh token and retrying once.
    """
    if credentials is None or not credentials.credentials:
        raise unauthorized("Not signed in.")

    try:
        return await auth.user_from_access_token(session, credentials.credentials)
    except AuthError as error:
        raise unauthorized(str(error)) from error


CurrentUser = Annotated[User, Depends(current_user)]
