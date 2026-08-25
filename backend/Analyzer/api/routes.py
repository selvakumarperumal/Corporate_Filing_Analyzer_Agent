"""The HTTP surface, assembled from the routers each domain owns.

Nothing is defined here except ``/api/health``, which belongs to no domain.
The routes themselves live with the code they are the front of:
:mod:`auth.routes`, :mod:`conversations.routes`, :mod:`analysis.routes`.
"""

from __future__ import annotations

from fastapi import APIRouter

from analysis.routes import router as filings_router
from auth.routes import router as auth_router
from conversations.routes import router as conversations_router
from core.config import settings

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(filings_router)
api_router.include_router(conversations_router)


@api_router.get("/api/health", tags=["health"])
async def health() -> dict[str, str]:
    """Report that the API is up and which models it is using.

    Deliberately open: a health check that needs a login cannot be used by the
    thing that has to know whether logins are working.
    """
    return {
        "status": "ok",
        "model": settings.OLLAMA_MODEL,
        "embedding_model": settings.OLLAMA_EMBEDDING_MODEL,
    }
