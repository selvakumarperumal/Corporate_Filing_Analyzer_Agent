"""Vector store — one isolated collection per chat session.

Every chat gets its own Chroma collection, so a new chat starts with nothing to
retrieve from and can never answer out of a previous chat's filings. Deleting a
chat drops its collection outright.

Collections outlive the process, because the conversations they belong to do:
a dossier reopened tomorrow still answers from the filing attached to it today.
What is cleared at startup is only what no conversation claims any more — see
:meth:`VectorService.prune_to`.

Where they live depends on one setting. Unset, ``CHROMA_HOST`` gives the
embedded store: a directory on this process's own disk, which is right for a
single instance and a bug for two — a filing ingested by one process is simply
not there for another, and sharing the directory between them corrupts the
SQLite index rather than sharing anything. Set it and the same code talks to
one Chroma server over HTTP, every instance a client of it, which is what lets
the API run more than one replica. See ``docs/SCALING.md``.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from core.config import settings
from core.paths import CHROMA_DIR

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = (".txt", ".md", ".csv")
SUPPORTED_EXTENSIONS = (".pdf", *TEXT_EXTENSIONS)

# Marks the collections we own, so leftovers can be cleared out on startup.
COLLECTION_PREFIX = "chat-"

_PERSIST_DIR = str(CHROMA_DIR)

# Open collection handles kept per process, so a busy dossier is not reopened
# on every question. Bounded because the key is a session id: unbounded, a
# long-lived instance accumulates one entry per dossier it has ever answered
# for, which is a slow leak nobody notices until the pod is weeks old.
_MAX_OPEN_COLLECTIONS = 512


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
        if settings.CHROMA_HOST:
            # A client of one shared server. Every instance sees the same
            # collections, so an upload handled by one and a question answered
            # by another agree about what has been filed.
            location = f"{settings.CHROMA_HOST}:{settings.CHROMA_PORT}"
            try:
                # Connects here rather than on first use — the constructor
                # heartbeats the server. Deliberate: a store that cannot be
                # reached is a broken deployment, and it should say so while
                # someone is watching the rollout, not on the first question.
                self._client = chromadb.HttpClient(
                    host=settings.CHROMA_HOST,
                    port=settings.CHROMA_PORT,
                    ssl=settings.CHROMA_SSL,
                )
            except Exception as error:
                raise RuntimeError(
                    f"Cannot reach the Chroma server at {location} "
                    f"(CHROMA_HOST/CHROMA_PORT): {error}. Start it before the "
                    f"API, or unset CHROMA_HOST to use the embedded store — "
                    f"which is single-instance only."
                ) from error
        else:
            # Embedded: a library reading this process's own disk. Correct for
            # one instance, and the reason a second one cannot simply be
            # started — see the module docstring.
            self._client = chromadb.PersistentClient(path=persist_directory)
            location = persist_directory
        self._shared = bool(settings.CHROMA_HOST)
        self._stores: OrderedDict[str, Chroma] = OrderedDict()

        logger.info(
            "VectorService ready (store=%s, shared=%s, chunk_size=%d, overlap=%d)",
            location,
            self._shared,
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
        store = self._stores.get(session_id)
        if store is not None:
            # Touched, so the dossiers being worked on now are the ones that
            # survive the eviction below.
            self._stores.move_to_end(session_id)
            return store

        store = Chroma(
            client=self._client,
            collection_name=self._collection_name(session_id),
            embedding_function=self.embeddings,
        )
        self._stores[session_id] = store
        if len(self._stores) > _MAX_OPEN_COLLECTIONS:
            # Dropping a handle costs one reopen, never a filing: the
            # collection itself is in the store, not in this dict.
            evicted, _ = self._stores.popitem(last=False)
            logger.debug("Closed least-recently-used collection handle %s", evicted)
        logger.debug("Opened collection for chat %s", session_id)
        return store

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
