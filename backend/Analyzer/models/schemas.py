"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from pydantic import BaseModel


class UploadResponse(BaseModel):
    """Response after a file upload."""
    status: str
    filename: str
    chunks_ingested: int
    session_id: str | None = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    model: str
    embedding_model: str


class CategoryInfo(BaseModel):
    """An analysis category the UI can route to directly."""
    id: str
    label: str
    description: str


class CategoryListResponse(BaseModel):
    """All categories exposed as graph entry points."""
    categories: list[CategoryInfo]


class QueryRequest(BaseModel):
    """Socket.IO `query` payload.

    `category` is either "auto" (LLM router decides) or a category id, which
    bypasses the router node entirely.
    """
    query: str = ""
    category: str = "auto"
    require_approval: bool = False
    session_id: str | None = None


class ResumeRequest(BaseModel):
    """Socket.IO `resume` payload for a paused human-in-the-loop run."""
    run_id: str
    category: str | None = None
