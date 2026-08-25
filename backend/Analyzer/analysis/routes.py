"""Filing ingest — the one HTTP route the analysis pipeline owns.

    POST /api/upload    ingest a filing into a dossier's collection

It sits here rather than with the other dossier routes because what it does is
index a document into the vector store; the row it then writes on the
conversation is the record of that, not the point of it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from analysis.pipeline import scoped_session_id
from api.dependencies import Analysis, CurrentUser, DbSession, History

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["filings"])


@router.post("/upload")
async def upload_file(
    user: CurrentUser,
    session: DbSession,
    analysis: Analysis,
    history: History,
    file: Annotated[UploadFile, File()],
    session_id: Annotated[str, Form()],
) -> dict[str, object]:
    """Ingest a filing (PDF, TXT, MD or CSV) into one dossier's collection.

    ``session_id`` is required: a filing always belongs to the dossier it was
    attached to, and is only ever retrieved for that dossier, under the account
    that uploaded it. Attaching a filing is also enough to open a dossier — an
    analyst may upload before they ask anything.
    """
    session_id = session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename given")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"{file.filename} is empty")

    try:
        result = await analysis.upload_file(
            content, file.filename, scoped_session_id(user.id, session_id)
        )
    except ValueError as error:
        # Unsupported file type, or a file we could not read any text out of.
        logger.warning("Upload rejected (%s): %s", file.filename, error)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        # Anything else — the embedding model being unreachable, say. Answered
        # as JSON carrying the reason, because the browser only has what this
        # response says to show the analyst.
        logger.exception("Upload failed (%s)", file.filename)
        raise HTTPException(
            status_code=500,
            detail=f"Could not add {file.filename}: {error}",
        ) from error

    # Recorded after the ingest, so the register never lists a filing that is
    # not actually searchable.
    conversation = await history.open_conversation(session, user.id, session_id)
    await history.record_filing(
        session, conversation, file.filename, int(result["chunks_ingested"])
    )

    logger.info(
        "Uploaded %s -> %d chunks (user=%s)",
        file.filename,
        result["chunks_ingested"],
        user.id,
    )
    # The scoped id is a backend detail; the browser gets back the id it sent.
    return {**result, "session_id": session_id}
