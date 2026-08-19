"""Vector store service for document ingestion and retrieval."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)

_DEFAULT_PERSIST_DIR = str(
    Path(__file__).resolve().parents[2] / "data" / "chroma_db"
)


class VectorService:
    """Handles document ingestion, chunking, and similarity retrieval."""

    def __init__(
        self,
        embeddings: Embeddings,
        persist_directory: str = _DEFAULT_PERSIST_DIR,
        collection_name: str = "corporate_filings",
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> None:
        self.embeddings = embeddings
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self.vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=embeddings,
            persist_directory=persist_directory,
        )
        # Chat sessions that have uploaded at least one document, so retrieval
        # can be scoped to "the files attached to this conversation".
        self._session_chunks: dict[str, int] = {}
        logger.info(
            "VectorService initialized (collection=%s, persist=%s)",
            collection_name,
            persist_directory,
        )

    async def ingest_documents(
        self,
        documents: list[Document],
        session_id: str | None = None,
    ) -> int:
        """Chunk and add documents to vector store. Returns chunk count."""
        chunks = self.splitter.split_documents(documents)
        if not chunks:
            return 0
        if session_id:
            for chunk in chunks:
                chunk.metadata["session_id"] = session_id
        await self.vectorstore.aadd_documents(chunks)
        if session_id:
            self._session_chunks[session_id] = (
                self._session_chunks.get(session_id, 0) + len(chunks)
            )
        logger.info("Ingested %d chunks from %d documents", len(chunks), len(documents))
        return len(chunks)

    async def ingest_file(
        self,
        file_bytes: bytes,
        filename: str,
        session_id: str | None = None,
    ) -> int:
        """Parse an uploaded file and ingest into vector store."""
        ext = Path(filename).suffix.lower()
        documents: list[Document] = []

        if ext == ".pdf":
            documents = await self._parse_pdf(file_bytes, filename)
        elif ext in (".txt", ".md", ".csv"):
            text = file_bytes.decode("utf-8", errors="replace")
            documents = [Document(page_content=text, metadata={"source": filename})]
        else:
            raise ValueError(f"Unsupported file type: {ext}")

        return await self.ingest_documents(documents, session_id=session_id)

    def has_session_documents(self, session_id: str | None) -> bool:
        """Whether this chat session has uploaded documents of its own."""
        return bool(session_id) and session_id in self._session_chunks

    async def search(
        self,
        query: str,
        k: int = 4,
        session_id: str | None = None,
    ) -> list[Document]:
        """Retrieve top-k relevant chunks, scoped to the session's uploads.

        Falls back to the whole collection when the session has not uploaded
        anything, so previously indexed filings stay queryable.
        """
        kwargs: dict = {}
        if self.has_session_documents(session_id):
            kwargs["filter"] = {"session_id": session_id}

        results = await self.vectorstore.asimilarity_search(query, k=k, **kwargs)
        if not results and kwargs:
            results = await self.vectorstore.asimilarity_search(query, k=k)
        logger.info("Retrieved %d chunks for query: %s", len(results), query[:50])
        return results

    async def _parse_pdf(self, file_bytes: bytes, filename: str) -> list[Document]:
        """Extract text from PDF bytes using pypdf."""
        from pypdf import PdfReader
        import io

        reader = PdfReader(io.BytesIO(file_bytes))
        documents = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                documents.append(
                    Document(
                        page_content=text,
                        metadata={"source": filename, "page": i + 1},
                    )
                )
        logger.info("Parsed %d pages from %s", len(documents), filename)
        return documents
