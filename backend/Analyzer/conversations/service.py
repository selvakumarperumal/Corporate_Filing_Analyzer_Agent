"""Conversation history — the ledger, and the slice of it a run is given.

Two histories come out of one table, and keeping them apart is the point of
this service:

*display history*
    everything, in order, paged for the dock. Nothing is trimmed out of it,
    because the analyst's record of what was asked and answered should not
    change shape as a dossier gets long.

*context history*
    what a run is actually sent: the last few turns, under a token budget,
    behind a rolling summary of whatever came before. Bounded on purpose — a
    dossier can run for hundreds of messages, and a prompt cannot.

The database is the source of truth. The cache in front of it
(:mod:`conversations.cache`) only ever serves the context read, and is allowed
to be absent, cold after a restart, or switched off entirely.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import aliased
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from conversations.cache import MessageCache, message_cache
from conversations.models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    STATUS_ERROR,
    STATUS_OK,
    Conversation,
    Message,
)
from core.config import settings
from core.leases import leases
from core.tokens import estimate_tokens
from db.columns import utcnow
from db.engine import SessionLocal

logger = logging.getLogger(__name__)

# How much of one message the summariser is shown. A single 20k-character
# report would otherwise fill the summariser's own context window.
_SUMMARY_CHARS_PER_MESSAGE = 1200

# How long one instance may hold the right to fold a conversation. Longer than
# a summarising call, so a slow model does not let a second instance start the
# same fold; short enough that an instance killed mid-fold blocks the next
# attempt for minutes rather than for ever.
_SUMMARY_LEASE_SECONDS = 600

# What a question gets for an answer when the run that should have produced one
# did not survive. See :meth:`HistoryService.sweep_interrupted_runs`.
INTERRUPTED_MESSAGE = (
    "This run was interrupted before it finished — the server restarted while "
    "the answer was being written. Ask again to get an answer."
)

# Interrupted runs healed per sweep. A backlog is unusual, and startup is not
# the place to work through an unbounded one; whatever is left is picked up by
# the next start.
_SWEEP_LIMIT = 200

# Folds ``(previous_summary, transcript) -> new summary``.
Summarizer = Callable[[str, str], Awaitable[str]]


@dataclass(slots=True)
class ContextHistory:
    """The conversation as a run is given it."""

    summary: str = ""
    messages: list[dict] = field(default_factory=list)
    tokens: int = 0

    def __bool__(self) -> bool:
        return bool(self.summary or self.messages)


class HistoryService:
    """Reads and writes the conversation ledger."""

    def __init__(
        self,
        cache: MessageCache | None = None,
        summarizer: Summarizer | None = None,
        context_messages: int = settings.HISTORY_CONTEXT_MESSAGES,
        context_tokens: int = settings.HISTORY_CONTEXT_TOKENS,
        summary_threshold: int = settings.HISTORY_SUMMARY_THRESHOLD,
    ) -> None:
        self.cache = cache if cache is not None else message_cache
        self.summarizer = summarizer
        self.context_messages = context_messages
        self.context_tokens = context_tokens
        self.summary_threshold = summary_threshold
        # Conversations currently being summarised, so a burst of questions
        # cannot start the same fold several times over.
        self._summarising: set[str] = set()
        # The tasks themselves. asyncio only holds a weak reference to a
        # running task, so a fold with no strong reference anywhere can be
        # garbage collected mid-await and simply never finish.
        self._summary_tasks: set[asyncio.Task] = set()

    def attach_summarizer(self, summarizer: Summarizer) -> None:
        """Give the service the LLM call it folds old turns with.

        Set after construction because the summariser needs the chat model,
        which is built later than this service is — until it arrives, long
        conversations simply keep their whole tail unsummarised.
        """
        self.summarizer = summarizer

    # ── Conversations ────────────────────────────────────────────────────

    async def open_conversation(
        self,
        session: AsyncSession,
        user_id: str,
        client_id: str,
        title: str = "",
    ) -> Conversation:
        """The analyst's conversation for ``client_id``, created if it is new.

        The browser mints the dossier id, so the first question in a dossier is
        also what brings its row into being. Always scoped by ``user_id``: an
        id from one account can never resolve to another's conversation.
        """
        conversation = await self.find(session, user_id, client_id)
        if conversation is not None:
            return conversation

        conversation = Conversation(
            user_id=user_id, client_id=client_id, title=title.strip()[:200]
        )
        session.add(conversation)
        try:
            await session.commit()
        except Exception:
            # Two questions raced into the same new dossier and the unique
            # constraint caught the second. The row the winner wrote is the one
            # both should use.
            await session.rollback()
            existing = await self.find(session, user_id, client_id)
            if existing is None:
                raise
            return existing

        await session.refresh(conversation)
        logger.info("Opened conversation %s (user=%s)", conversation.id, user_id)
        return conversation

    async def find(
        self, session: AsyncSession, user_id: str, client_id: str
    ) -> Conversation | None:
        """One conversation of this analyst's, by the id their browser gave it."""
        result = await session.exec(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .where(Conversation.client_id == client_id)
        )
        return result.first()

    async def list_conversations(
        self, session: AsyncSession, user_id: str, limit: int = 100
    ) -> list[Conversation]:
        """An analyst's dossiers, most recently spoken in first."""
        result = await session.exec(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(col(Conversation.last_message_at).desc())
            .limit(limit)
        )
        return list(result.all())

    async def set_title(
        self, session: AsyncSession, conversation: Conversation, title: str
    ) -> Conversation:
        """Name a dossier. Blank titles are ignored rather than stored."""
        title = title.strip()[:200]
        if not title or title == conversation.title:
            return conversation

        conversation.title = title
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return conversation

    async def record_filing(
        self,
        session: AsyncSession,
        conversation: Conversation,
        name: str,
        chunks: int,
    ) -> Conversation:
        """Note a filing that was ingested into this dossier's collection.

        The register is what the dock shows a returning analyst; the filing's
        text lives in the vector store, not here.
        """
        filings = list(conversation.filings or [])
        filings.append(
            {"name": name, "chunks": chunks, "added_at": utcnow().isoformat()}
        )
        # Reassigned rather than mutated in place: SQLAlchemy tracks a JSON
        # column by identity, and an appended-to list looks unchanged to it.
        conversation.filings = filings
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return conversation

    async def delete_conversation(
        self, session: AsyncSession, user_id: str, client_id: str
    ) -> bool:
        """Discard a dossier and every message in it. Messages cascade."""
        conversation = await self.find(session, user_id, client_id)
        if conversation is None:
            return False

        conversation_id = conversation.id
        await session.delete(conversation)
        await session.commit()
        await self.cache.drop(conversation_id)
        logger.info("Deleted conversation %s (user=%s)", conversation_id, user_id)
        return True

    # ── Messages ─────────────────────────────────────────────────────────

    async def record_message(
        self,
        session: AsyncSession,
        conversation: Conversation,
        role: str,
        content: str,
        status: str = STATUS_OK,
        meta: dict | None = None,
    ) -> Message:
        """Append one message to the ledger, and to the cached tail.

        Written in the same transaction as the conversation's counters, so the
        row count and ``message_count`` cannot disagree.

        The conversation row is locked first, and that is not belt and braces.
        A position is claimed by reading the highest one and writing the next,
        and two writers doing that at once compute the same number —
        ``uq_message_position`` then turns the loser's insert into an
        IntegrityError. On the answer path that error is swallowed
        (:func:`api.socket._record_answer` will not fail a request over it), so
        the symptom is an answer the analyst watched arrive and cannot find
        afterwards. Two browser tabs are enough at one instance; several
        instances make it ordinary.
        """
        # Held until this transaction commits, so the read of the last position
        # and the write of the next one are one indivisible step — across
        # processes, since the lock lives in Postgres.
        await session.exec(
            select(Conversation.id)
            .where(Conversation.id == conversation.id)
            .with_for_update()
        )

        meta = dict(meta or {})
        if role == ROLE_USER and "run" not in meta:
            # The ledger numbers runs, not messages, and a client that has
            # paged back into an old dossier has no way to work out where in
            # the count it is standing. Stamped once, at write time.
            meta["run"] = await self._run_number(session, conversation.id)

        next_seq = await self._next_seq(session, conversation.id)
        message = Message(
            conversation_id=conversation.id,
            seq=next_seq,
            role=role,
            content=content,
            tokens=estimate_tokens(content),
            status=status,
            meta=meta,
        )
        conversation.message_count = next_seq
        conversation.last_message_at = message.created_at

        session.add(message)
        session.add(conversation)
        await session.commit()
        await session.refresh(message)

        await self.cache.append(conversation.id, _cacheable(message))
        return message

    async def page_messages(
        self,
        session: AsyncSession,
        conversation: Conversation,
        limit: int = settings.HISTORY_PAGE_SIZE,
        before_seq: int | None = None,
    ) -> list[Message]:
        """Display history: one page, oldest first, newest page by default.

        Paged from the end backwards — opening a dossier should show the last
        thing said, not the first — and cursored on ``seq`` rather than an
        offset, so a message arriving mid-scroll cannot shift the page under
        the reader.

        Pages are aligned to whole runs: an answer whose question fell on the
        previous page is left for that page to carry, so a client can pair
        question with answer within one page and never has to stitch a run back
        together across two. Costs at most one message off the front of a page.
        """
        query = select(Message).where(Message.conversation_id == conversation.id)
        if before_seq is not None:
            query = query.where(col(Message.seq) < before_seq)

        result = await session.exec(
            query.order_by(col(Message.seq).desc()).limit(limit)
        )
        messages = list(reversed(result.all()))

        # Not when it would empty the page — a caller asking for one message at
        # a time should still make progress.
        while len(messages) > 1 and messages[0].role != ROLE_USER:
            messages.pop(0)
        return messages

    async def context_for(
        self, session: AsyncSession, conversation: Conversation
    ) -> ContextHistory:
        """Context history: the summary plus the tail that fits the budget.

        Four things narrow it, in order — anything already folded into the
        summary is dropped, then the runs that failed, then all but the last
        ``context_messages`` turns, then whatever does not fit
        ``context_tokens``. The token pass works off the counts stored with
        each message, which is what they are for.
        """
        tail = await self._recent(session, conversation)

        fresh = [
            m
            for m in tail
            if m["seq"] > conversation.summary_through_seq
            # A failed run is kept in the ledger so the analyst can see it, but
            # conditioning the next answer on an error message would only
            # teach the model to apologise.
            and m.get("status", STATUS_OK) == STATUS_OK
        ]
        # A question whose run failed is dropped along with the failure: on its
        # own it reads as something the analyzer was asked and declined to
        # answer, which is not what happened and not worth conditioning on.
        answered = [
            message
            for index, message in enumerate(fresh)
            if message["role"] != ROLE_USER
            or (
                index + 1 < len(fresh)
                and fresh[index + 1]["role"] == ROLE_ASSISTANT
            )
        ]

        window = answered[-self.context_messages :]

        # Newest first while trimming, so what is dropped is always the oldest.
        budget = self.context_tokens - estimate_tokens(conversation.summary)
        kept: list[dict] = []
        used = 0
        for message in reversed(window):
            cost = message.get("tokens") or estimate_tokens(message["content"])
            if kept and used + cost > budget:
                break
            kept.append(message)
            used += cost

        kept.reverse()
        history = ContextHistory(
            summary=conversation.summary, messages=kept, tokens=used
        )
        logger.debug(
            "Context for %s: %d/%d message(s), ~%d tokens, summary=%s",
            conversation.id,
            len(kept),
            len(tail),
            used,
            bool(conversation.summary),
        )
        return history

    async def _recent(
        self, session: AsyncSession, conversation: Conversation
    ) -> list[dict]:
        """The conversation's hot tail, from the cache if it is there."""
        cached = await self.cache.recent(conversation.id)
        if cached is not None:
            return _in_order(cached)

        window = max(self.context_messages, settings.REDIS_HOT_WINDOW)
        result = await session.exec(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(col(Message.seq).desc())
            .limit(window)
        )
        tail = [_cacheable(m) for m in reversed(result.all())]
        await self.cache.prime(conversation.id, tail)
        return tail

    async def _run_number(self, session: AsyncSession, conversation_id: str) -> int:
        """Which run the question about to be recorded opens, counting from 1."""
        result = await session.exec(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
            .where(Message.role == ROLE_USER)
        )
        return (result.one() or 0) + 1

    async def _next_seq(self, session: AsyncSession, conversation_id: str) -> int:
        """The next free position in a conversation.

        Read from the messages themselves rather than the conversation's
        counter, so a counter that has drifted cannot hand out a position that
        is already taken.
        """
        result = await session.exec(
            select(col(Message.seq))
            .where(Message.conversation_id == conversation_id)
            .order_by(col(Message.seq).desc())
            .limit(1)
        )
        return (result.first() or 0) + 1

    # ── Interrupted runs ─────────────────────────────────────────────────

    async def sweep_interrupted_runs(
        self, older_than_minutes: int = settings.STALE_RUN_MINUTES
    ) -> int:
        """Give an answer to questions whose run never came back.

        A question is written before the graph starts and its answer after the
        stream ends. Kill the process in between — a SIGKILL past the grace
        period, a lost node — and only the first row exists, so the dossier
        shows a question the analyst will wait for forever.

        Shutdown drains in-flight runs, so this is for the deaths that gave no
        warning. Run once per start, under an advisory lock, against questions
        old enough that no run could still be working on one: the cutoff must
        stay well clear of a slow answer on another instance, because marking a
        live run as interrupted would be worse than the problem.

        Returns how many were healed.
        """
        cutoff = utcnow() - timedelta(minutes=older_than_minutes)
        later = aliased(Message)

        async with SessionLocal() as session:
            result = await session.exec(
                select(Message)
                .where(Message.role == ROLE_USER)
                .where(col(Message.created_at) < cutoff)
                # Nothing was ever written after it — not an answer, not
                # another question. Anything else means the run landed.
                .where(
                    ~select(later.id)
                    .where(later.conversation_id == Message.conversation_id)
                    .where(col(later.seq) > Message.seq)
                    .exists()
                )
                .order_by(col(Message.created_at))
                .limit(_SWEEP_LIMIT)
            )
            stranded = list(result.all())

        healed = 0
        for question in stranded:
            try:
                async with SessionLocal() as session:
                    conversation = await session.get(
                        Conversation, question.conversation_id
                    )
                    if conversation is None:
                        continue
                    # Re-checked under the row lock `record_message` takes: an
                    # instance that started answering between the query above
                    # and now would have written the row we are about to
                    # duplicate.
                    if conversation.message_count > question.seq:
                        continue

                    await self.record_message(
                        session,
                        conversation,
                        ROLE_ASSISTANT,
                        INTERRUPTED_MESSAGE,
                        status=STATUS_ERROR,
                        meta={"error": "interrupted by a restart"},
                    )
                healed += 1
            except Exception:
                # One stubborn conversation should not stop the sweep, and the
                # sweep should never stop a startup.
                logger.exception(
                    "Could not close out the interrupted run in conversation %s",
                    question.conversation_id,
                )

        if healed:
            logger.info("Closed out %d interrupted run(s) from a previous life", healed)
        return healed

    # ── Rolling summary ──────────────────────────────────────────────────

    def schedule_summary(self, conversation_id: str) -> None:
        """Fold this conversation's older turns, off the critical path.

        Fired after an answer has been delivered rather than before the next
        one is asked for: summarising is another LLM call, and no analyst
        should wait on last week's history being compressed.
        """
        if self.summarizer is None or conversation_id in self._summarising:
            return

        self._summarising.add(conversation_id)
        task = asyncio.create_task(self._summarise(conversation_id))
        self._summary_tasks.add(task)

        def _finished(task: asyncio.Task) -> None:
            self._summarising.discard(conversation_id)
            self._summary_tasks.discard(task)

        task.add_done_callback(_finished)

    async def _summarise(self, conversation_id: str) -> None:
        """Compress everything but the recent tail into the rolling summary.

        Three phases, and the shape is deliberate: read what is needed, close
        the session, call the model, then reopen to write. Holding a pooled
        connection across a call that takes tens of seconds is how a handful of
        background folds exhaust a pool of ten and leave analysts waiting on a
        connection to ask their next question.

        The lease is what stops two instances folding the same dossier. It is
        best effort — with no Redis, ``_summarising`` still dedupes within one
        process, and the worst a duplicate fold costs is a wasted model call
        against the same three columns, last write winning.
        """
        lease = f"summary:{conversation_id}"
        if not await leases.acquire(lease, _SUMMARY_LEASE_SECONDS):
            logger.debug(
                "Another instance is already summarising %s — leaving it to them",
                conversation_id,
            )
            return

        try:
            # ── Read ─────────────────────────────────────────────────────
            async with SessionLocal() as session:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:
                    return

                previous_summary = conversation.summary
                result = await session.exec(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .where(col(Message.seq) > conversation.summary_through_seq)
                    .order_by(col(Message.seq))
                )
                pending = list(result.all())
                if len(pending) <= self.summary_threshold:
                    return

                # The recent turns stay verbatim — they are what the next
                # question is most likely to be about.
                fold = pending[: -self.context_messages] or pending
                through_seq = fold[-1].seq
                folded = len(fold)
                # Rendered before the session closes, while the rows are still
                # attached and their columns loaded.
                transcript = _transcript(fold)

            # ── Fold, holding nothing ────────────────────────────────────
            summary = (await self.summarizer(previous_summary, transcript)).strip()
            if not summary:
                logger.warning(
                    "Summariser returned nothing for %s — leaving history as it is",
                    conversation_id,
                )
                return

            # ── Write ────────────────────────────────────────────────────
            async with SessionLocal() as session:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:  # deleted while the model was working
                    return
                if conversation.summary_through_seq >= through_seq:
                    # Someone folded at least this far while we were away.
                    # Ours is not newer, only later, so it is discarded rather
                    # than allowed to walk the summary backwards.
                    logger.debug(
                        "Discarding a stale fold of %s (already summarised through %d)",
                        conversation_id,
                        conversation.summary_through_seq,
                    )
                    return

                conversation.summary = summary
                conversation.summary_through_seq = through_seq
                conversation.summary_tokens = estimate_tokens(summary)
                session.add(conversation)
                await session.commit()

            logger.info(
                "Summarised %s through seq %d (%d message(s) folded)",
                conversation_id,
                through_seq,
                folded,
            )
        except Exception:
            # A conversation that cannot be summarised still answers; it just
            # carries a longer unsummarised tail until the next attempt.
            logger.exception("Summarising conversation %s failed", conversation_id)
        finally:
            # Given back rather than left to expire, so the next question in
            # this dossier can fold immediately if it needs to.
            await leases.release(lease)


def _in_order(messages: list[dict]) -> list[dict]:
    """The tail by position, one entry per position.

    The cache is appended to by whichever instance recorded the message, after
    its own commit — so two instances writing to one dossier can push their
    rows in the opposite order to the one they were assigned, and a retried
    write can push the same row twice. Neither is worth a lock over a cache:
    sorting a few dozen entries by ``seq`` costs nothing and means a reader
    never has to trust how they arrived.
    """
    by_seq = {message["seq"]: message for message in messages}
    return [by_seq[seq] for seq in sorted(by_seq)]


def _cacheable(message: Message) -> dict:
    """The parts of a message a run needs, small enough to cache."""
    return {
        "seq": message.seq,
        "role": message.role,
        "content": message.content,
        "tokens": message.tokens,
        "status": message.status,
    }


def _transcript(messages: list[Message]) -> str:
    """Render messages as a transcript for the summariser."""
    speaker = {ROLE_USER: "Analyst", ROLE_ASSISTANT: "Analyzer"}
    lines = []
    for message in messages:
        content = message.content.strip()
        if len(content) > _SUMMARY_CHARS_PER_MESSAGE:
            content = content[:_SUMMARY_CHARS_PER_MESSAGE].rstrip() + " […]"
        lines.append(f"{speaker.get(message.role, message.role.title())}: {content}")
    return "\n\n".join(lines)


history_service = HistoryService()
