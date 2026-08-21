"""Shared dependencies and the long-lived services the app is built from.

The services are singletons because they own things that should exist once per
process: the chat model and its vector store, the message cache, the graph.
They live here rather than in :mod:`main` so the routers can reach them without
importing the app module they are mounted on.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel.ext.asyncio.session import AsyncSession

from core.config import settings
from core.database import get_session
from core.security import AuthError
from models.user import User
from services.auth_service import AuthService
from services.chat_service import ChatService
from services.history_service import HistoryService, history_service

# auto_error=False so a missing header comes back as our own 401 with a
# WWW-Authenticate hint, rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)

auth_service = AuthService()

chat_service = ChatService(
    model=settings.OLLAMA_MODEL,
    embedding_model=settings.OLLAMA_EMBEDDING_MODEL,
    base_url=settings.OLLAMA_BASE_URL,
)

# The ledger reads and writes; the summariser it folds old turns with comes
# from the same model that answers, so it is attached once the chat service
# exists rather than at import of the history module.
history_service.attach_summarizer(chat_service.summary.summarise)

DbSession = Annotated[AsyncSession, Depends(get_session)]


# The services are process-wide singletons, so these providers just hand the
# same instance to every request — the point is not lifecycle but the seam:
# a route that asks for its services through Depends can have them swapped in
# a test with `app.dependency_overrides`, which a module-level import cannot.
def get_auth_service() -> AuthService:
    """The account service: signup, login, refresh, token verification."""
    return auth_service


def get_chat_service() -> ChatService:
    """The analysis pipeline and the vector store behind it."""
    return chat_service


def get_history_service() -> HistoryService:
    """The conversation ledger — messages, filings, rolling summaries."""
    return history_service


Auth = Annotated[AuthService, Depends(get_auth_service)]
Chat = Annotated[ChatService, Depends(get_chat_service)]
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
