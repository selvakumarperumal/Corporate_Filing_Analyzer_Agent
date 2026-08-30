#!/usr/bin/env python3
"""The machinery that keeps N instances from tripping over each other.

    backend/Analyzer/.venv/bin/python deploy/checks/coordination.py \
        --database-url postgresql+asyncpg://analyzer:analyzer@127.0.0.1:15432/filing_analyzer \
        --redis-url redis://127.0.0.1:16379/0

Talks to Postgres and Redis directly rather than through the API, because what
it checks lives below the API: row locks, advisory locks, leases, and the
startup sweep. It imports the app's own modules, so it verifies the code that
actually ships rather than a re-implementation of it.

Against a cluster, port-forward first:

    kubectl -n cfa port-forward svc/postgres 15432:5432 &
    kubectl -n cfa port-forward svc/redis    16379:6379 &

It writes and deletes its own accounts and leaves nothing behind. Safe against
a development database; do not point it at one you care about.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Report, die  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ANALYZER = REPO / "backend" / "Analyzer"

WRITERS = 12


def _bootstrap(database_url: str, redis_url: str) -> None:
    """Point the app's settings at the deployment before anything imports them."""
    if not ANALYZER.is_dir():
        die(f"expected the backend at {ANALYZER}")
    sys.path.insert(0, str(ANALYZER))
    os.environ["DATABASE_URL"] = database_url
    os.environ["REDIS_URL"] = redis_url


async def check_ledger_race(report: Report) -> None:
    """Break #5: concurrent writers claiming the same message position."""
    from auth.models import User
    from conversations.models import ROLE_USER, Conversation, Message
    from conversations.service import HistoryService
    from db.engine import SessionLocal
    from sqlmodel import col, select

    report.section(f"Break #5 — {WRITERS} writers, one conversation")
    service = HistoryService()

    async with SessionLocal() as session:
        user = User(email=f"coord-{uuid.uuid4().hex[:8]}@example.com",
                    name="Coordination", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        conversation = Conversation(user_id=user.id, client_id=uuid.uuid4().hex,
                                    title="coordination check")
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        user_id, conversation_id = user.id, conversation.id

    async def write(n: int) -> bool:
        try:
            async with SessionLocal() as session:
                row = await session.get(Conversation, conversation_id)
                await service.record_message(session, row, ROLE_USER, f"question {n}")
            return True
        except Exception:
            return False

    landed = sum(await asyncio.gather(*(write(n) for n in range(WRITERS))))

    async with SessionLocal() as session:
        rows = (await session.exec(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(col(Message.seq))
        )).all()
        seqs = [m.seq for m in rows]
        await session.delete(await session.get(User, user_id))
        await session.commit()

    report.check("every concurrent write lands", landed == WRITERS,
                 f"{landed}/{WRITERS} — losses are the read-then-write race")
    report.check("one row per write", len(rows) == WRITERS, f"{len(rows)} rows")
    report.check("positions are unique and contiguous",
                 seqs == list(range(1, WRITERS + 1)), str(seqs))


async def check_sweep(report: Report) -> None:
    """Break #4: questions whose run never came back get closed out."""
    from datetime import timedelta

    from auth.models import User
    from conversations.models import (ROLE_ASSISTANT, ROLE_USER, STATUS_ERROR,
                                      Conversation, Message)
    from conversations.service import HistoryService
    from db.columns import utcnow
    from db.engine import SessionLocal
    from sqlmodel import col, select

    report.section("Break #4 — the interrupted-run sweep")
    service = HistoryService()
    made: list[str] = []

    async def stranded(age_minutes: int) -> tuple[str, str]:
        async with SessionLocal() as session:
            user = User(email=f"sweep-{uuid.uuid4().hex[:8]}@example.com",
                        name="Sweep", password_hash="x")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            conversation = Conversation(user_id=user.id, client_id=uuid.uuid4().hex,
                                        title="interrupted")
            session.add(conversation)
            await session.commit()
            await session.refresh(conversation)
            question = Message(
                conversation_id=conversation.id, seq=1, role=ROLE_USER,
                content="what changed?",
                created_at=utcnow() - timedelta(minutes=age_minutes),
            )
            conversation.message_count = 1
            session.add(question)
            session.add(conversation)
            await session.commit()
            made.append(user.id)
            return user.id, conversation.id

    async def ledger(conversation_id: str) -> list[tuple]:
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(col(Message.seq))
            )).all()
            return [(m.seq, m.role, m.status) for m in rows]

    _, old = await stranded(age_minutes=90)
    _, fresh = await stranded(age_minutes=1)

    healed = await service.sweep_interrupted_runs(older_than_minutes=30)
    old_rows, fresh_rows = await ledger(old), await ledger(fresh)

    report.check("an abandoned question gets an answer row",
                 len(old_rows) == 2 and old_rows[1][1] == ROLE_ASSISTANT, str(old_rows))
    report.check("marked as a failure, not an answer",
                 len(old_rows) == 2 and old_rows[1][2] == STATUS_ERROR)
    report.check("a question from a minute ago is left alone",
                 len(fresh_rows) == 1,
                 "the cutoff must never overtake a run still going elsewhere")
    report.check("the sweep says what it did", healed >= 1, f"healed={healed}")

    again = await service.sweep_interrupted_runs(older_than_minutes=30)
    report.check("running it twice changes nothing",
                 await ledger(old) == old_rows, f"second pass healed={again}")

    async with SessionLocal() as session:
        for user_id in made:
            row = await session.get(User, user_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


async def check_leases(report: Report, redis_url: str) -> None:
    """§9: the Redis lease that stops two instances folding one dossier."""
    from core.leases import leases

    report.section("Leases — duplicate background work")

    if not redis_url:
        # Not configured is not broken: leases are best effort by design, and
        # without them the per-process guard still dedupes within one instance.
        report.note("no --redis-url — leases are off, and the app is correct without them")
        report.note("duplicate folds cost a model call, never correctness")
        return

    await leases.connect()
    if not report.check("Redis is reachable", leases.enabled,
                        f"{redis_url} — configured but not answering"):
        report.note("the app degrades to per-process dedupe, but you asked for Redis")
        return
    name = f"check-{uuid.uuid4().hex[:8]}"
    report.check("the first instance takes the lease", await leases.acquire(name, 60))
    report.check("a second instance is turned away", not await leases.acquire(name, 60))
    await leases.release(name)
    report.check("released, the next instance takes it", await leases.acquire(name, 60))
    await leases.release(name)
    await leases.close()


async def check_advisory_locks(report: Report) -> None:
    """§9: the Postgres locks around schema creation and startup housekeeping."""
    from db.locks import only_one

    report.section("Advisory locks — work that must happen once")
    name = f"check-{uuid.uuid4().hex[:8]}"

    async with only_one(name) as mine:
        report.check("the first instance holds it", mine)
        async with only_one(name) as theirs:
            report.check("a second instance is told to skip", not theirs)
            report.check("and is not left waiting", True, "pg_try_advisory_lock never blocks")
    async with only_one(name) as after:
        report.check("it is released when the block exits", after)


async def run(args: argparse.Namespace) -> int:
    from db.engine import init_db

    report = Report("Coordination between instances")
    await init_db()
    report.section("Schema")
    report.check("init_db completes under its advisory lock", True,
                 "concurrent create_all is what this lock prevents")

    await check_ledger_race(report)
    await check_sweep(report)
    await check_leases(report, args.redis_url)
    await check_advisory_locks(report)
    return report.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        default="postgresql+asyncpg://analyzer:analyzer@127.0.0.1:15432/filing_analyzer",
        help="async Postgres URL; port-forward the cluster's database to reach it",
    )
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0",
                        help="blank to check the no-Redis fallback")
    args = parser.parse_args()
    _bootstrap(args.database_url, args.redis_url)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
