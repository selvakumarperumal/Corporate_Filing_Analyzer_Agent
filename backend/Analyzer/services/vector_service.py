"""Vector store — one isolated collection per chat session.

Every chat gets its own Chroma collection, so a new chat starts with nothing to
retrieve from and can never answer out of a previous chat's filings. Deleting a
chat drops its collection outright.

Collections outlive the process, because the conversations they belong to do:
a dossier reopened tomorrow still answers from the filing attached to it today.
What is cleared at startup is only what no conversation claims any more — see
:meth:`VectorService.prune_to`.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Iterable
from pathlib import Path

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = (".txt", ".md", ".csv")
SUPPORTED_EXTENSIONS = (".pdf", *TEXT_EXTENSIONS)

# Marks the collections we own, so leftovers can be cleared out on startup.
COLLECTION_PREFIX = "chat-"

_PERSIST_DIR = str(Path(__file__).resolve().parents[2] / "data" / "chroma_db")


class VectorService:
    """Ingests uploaded filings and searches them back, scoped to one chat."""

    def __init__(
        self,
        embeddings: Embeddings,
        persist_directory: str = _PERSIST_DIR,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> None:
        self.embeddings = embeddings
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self._client = chromadb.PersistentClient(path=persist_directory)
        self._stores: dict[str, Chroma] = {}

        logger.info(
            "VectorService ready (persist=%s, chunk_size=%d, overlap=%d)",
            persist_directory,
            chunk_size,
            chunk_overlap,
        )

    async def ingest_file(
        self,
        file_bytes: bytes,
        filename: str,
        session_id: str,
    ) -> int:
        """Add an uploaded file to this chat's collection. Returns chunk count.

        Raises:
            ValueError: if the file type is unsupported or holds no text.
        """
        extension = Path(filename).suffix.lower()
        logger.debug(
            "Ingesting %s (%d bytes) into chat %s", filename, len(file_bytes), session_id
        )

        if extension == ".pdf":
            documents = self._read_pdf(file_bytes, filename)
        elif extension in TEXT_EXTENSIONS:
            text = file_bytes.decode("utf-8", errors="replace")
            documents = [Document(page_content=text, metadata={"source": filename})]
        else:
            raise ValueError(
                f"Unsupported file type '{extension}'. "
                f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
            )

        chunks = self.splitter.split_documents(documents)
        if not chunks:
            raise ValueError(f"No readable text found in {filename}")

        await self._store(session_id).aadd_documents(chunks)
        logger.info("Ingested %s -> %d chunks (chat=%s)", filename, len(chunks), session_id)
        return len(chunks)

    async def search(self, query: str, session_id: str, k: int = 4) -> list[Document]:
        """Return the top-k chunks from this chat's own filings.

        No fallback to other chats: if nothing has been uploaded here, the
        answer is built without a filing rather than out of someone else's.
        """
        if not session_id:
            logger.warning("Search with no session id — returning no context")
            return []

        results = await self._store(session_id).asimilarity_search(query, k=k)
        logger.info(
            "Retrieved %d chunk(s) for %r (chat=%s)", len(results), query[:50], session_id
        )
        return results

    def delete_session(self, session_id: str) -> bool:
        """Drop this chat's collection. Returns whether there was one to drop."""
        self._stores.pop(session_id, None)
        name = self._collection_name(session_id)
        try:
            self._client.delete_collection(name)
        except Exception:
            logger.debug("Nothing to delete for chat %s", session_id)
            return False
        logger.info("Deleted filings for chat %s", session_id)
        return True

    def _store(self, session_id: str) -> Chroma:
        """Open (once per session) the collection holding this chat's filings."""
        if session_id not in self._stores:
            self._stores[session_id] = Chroma(
                client=self._client,
                collection_name=self._collection_name(session_id),
                embedding_function=self.embeddings,
            )
            logger.debug("Opened collection for chat %s", session_id)
        return self._stores[session_id]

    def _collection_name(self, session_id: str) -> str:
        """Chroma-safe collection name for a session.

        Hashed because Chroma restricts names to a length and character set the
        client's session ids are not obliged to respect.
        """
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        return f"{COLLECTION_PREFIX}{digest}"

    def prune_to(self, session_ids: Iterable[str]) -> int:
        """Drop every chat collection not named by ``session_ids``.

        Run at startup against the conversations still in the database. What it
        removes is genuinely orphaned: filings whose dossier was deleted while
        the process was down, or left behind by a crash between ingesting a
        file and recording it. Everything an analyst can still open is kept.

        Returns the number of collections dropped.
        """
        live = {self._collection_name(session_id) for session_id in session_ids}
        stale = [
            collection.name
            for collection in self._client.list_collections()
            if collection.name.startswith(COLLECTION_PREFIX) and collection.name not in live
        ]

        for name in stale:
            try:
                self._client.delete_collection(name)
            except Exception:
                # Losing one is not worth failing startup over — it costs disk,
                # and nothing can reach it without the session id that names it.
                logger.warning("Could not drop orphaned collection %s", name, exc_info=True)

        if stale:
            logger.info(
                "Cleared %d orphaned collection(s); %d dossier(s) kept",
                len(stale),
                len(live),
            )
        return len(stale)

    def _read_pdf(self, file_bytes: bytes, filename: str) -> list[Document]:
        """Extract one document per non-empty PDF page.

        Every way a PDF can defeat the parser — truncated, malformed, password
        protected — comes back as a ValueError naming the reason, so the caller
        can tell the analyst what is wrong with their file instead of failing
        with a stack trace.
        """
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError

        try:
            reader = PdfReader(io.BytesIO(file_bytes))
        except PyPdfError as error:
            logger.warning("Could not open %s as a PDF: %s", filename, error)
            raise ValueError(
                f"{filename} is not a readable PDF — the file looks corrupt or "
                f"incomplete."
            ) from error

        if reader.is_encrypted:
            # An empty password opens the common "printing restricted" case; a
            # genuinely locked file does not.
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                raise ValueError(
                    f"{filename} is password protected. Remove the password and "
                    f"upload it again."
                )

        documents: list[Document] = []
        unreadable_pages = 0
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as error:
                # One broken page should not cost the analyst the whole filing.
                unreadable_pages += 1
                logger.warning("Skipped page %d of %s: %s", number, filename, error)
                continue
            if text:
                documents.append(
                    Document(
                        page_content=text,
                        metadata={"source": filename, "page": number},
                    )
                )

        if not documents:
            raise ValueError(
                f"No readable text found in {filename}. Scanned PDFs need to be "
                f"run through OCR before they can be analyzed."
            )

        logger.info(
            "Parsed %d page(s) with text from %s (%d unreadable)",
            len(documents),
            filename,
            unreadable_pages,
        )
        return documents
