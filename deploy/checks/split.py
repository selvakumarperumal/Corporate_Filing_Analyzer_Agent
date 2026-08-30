#!/usr/bin/env python3
"""Two instances, and the three things that break between them.

    python deploy/checks/split.py --a http://127.0.0.1:18001 \
                                  --b http://127.0.0.1:18002

Point it at two *different* instances. On Kubernetes that means one
port-forward each, because the whole value of this check is choosing which
instance does what — left to a load balancer, both halves land on the same pod
most of the time and the check passes without proving anything:

    PODS=($(kubectl -n cfa get pods -l app.kubernetes.io/name=cfa-backend \
            -o jsonpath='{.items[*].metadata.name}'))
    kubectl -n cfa port-forward pod/${PODS[0]} 18001:8000 &
    kubectl -n cfa port-forward pod/${PODS[1]} 18002:8000 &

Under compose with `--scale backend=2`, publish two host ports instead.

What it proves, in docs/SCALING.md's numbering:

    Break #1  a token minted by A is accepted by B
    Break #2  a filing ingested by A is retrievable by B
    Break #5  A and B writing one dossier at once lose nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Report, die  # noqa: E402

try:
    import aiohttp
    import socketio
except ImportError as error:  # pragma: no cover
    die(f"{error.name} is missing — run this with backend/Analyzer/.venv/bin/python")

REPO = Path(__file__).resolve().parents[2]
DEFAULT_FILING = REPO / "mock_10k_filing.txt"


async def reachable(http: "aiohttp.ClientSession", base: str) -> tuple[bool, str]:
    """Is there an API there? Returns (ok, something short to print)."""
    try:
        async with http.get(f"{base}/api/health", timeout=aiohttp.ClientTimeout(total=10)) as r:
            return r.status == 200, f"HTTP {r.status}"
    except Exception as error:
        return False, type(error).__name__


async def ask(base: str, token: str, question: str, dossier: str,
              headers: dict, timeout: float) -> tuple[str, list[str]]:
    """One question over a socket on ``base``. Returns (answer, errors)."""
    sio = socketio.AsyncClient()
    chunks: list[str] = []
    errors: list[str] = []
    finished = asyncio.Event()

    @sio.on("token")
    async def _token(data): chunks.append(data.get("content", ""))
    @sio.on("done")
    async def _done(data): finished.set()
    @sio.on("error")
    async def _error(data): errors.append(data.get("message", "")); finished.set()

    await sio.connect(base, auth={"token": token}, headers=headers,
                      transports=["websocket"], wait_timeout=20)
    await sio.emit("query", {"query": question, "session_id": dossier,
                             "title": "", "files": []})
    try:
        await asyncio.wait_for(finished.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        errors.append(f"no answer within {timeout}s")
    await sio.disconnect()
    return "".join(chunks), errors


async def run(args: argparse.Namespace) -> int:
    report = Report(f"Two instances — A={args.a}  B={args.b}")
    headers = {"Origin": args.origin}
    email = f"split-{uuid.uuid4().hex[:8]}@example.com"
    dossier = uuid.uuid4().hex

    report.section("Both instances are up, and they are two")
    async with aiohttp.ClientSession(headers=headers) as http:
        a_up, a_why = await reachable(http, args.a)
        b_up, b_why = await reachable(http, args.b)
        if not report.check("A and B both answer /api/health", a_up and b_up,
                            f"A: {a_why} | B: {b_why}"):
            report.note("a dead port-forward is the usual cause; restart it and retry")
            return report.finish()
        if args.a == args.b:
            report.check("A and B are different endpoints", False,
                         "same URL twice — this check would prove nothing")
            return report.finish()

        report.section("Break #1 — one signing key")

        async with http.post(
            f"{args.a}/api/auth/signup",
            json={"email": email, "password": "Sup3rSecret!23", "name": "Split"},
        ) as r:
            body = await r.json()
        if not report.check("A mints a token", r.status == 201, str(body)[:120]):
            return report.finish()
        token = body["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        async with http.get(f"{args.b}/api/auth/me", headers=auth) as r:
            who = await r.json()
        if not report.check("B accepts A's token", r.status == 200,
                            "JWT_SECRET_KEY differs between instances"
                            if r.status != 200 else who.get("email", "")):
            report.note("set JWT_SECRET_KEY to the same value on every instance")
            return report.finish()

        report.section("Break #2 — one vector store")

        filing = Path(args.filing)
        form = aiohttp.FormData()
        form.add_field("session_id", dossier)
        form.add_field("file", filing.read_bytes(), filename=filing.name,
                       content_type="text/plain")
        async with http.post(f"{args.a}/api/upload", data=form, headers=auth) as r:
            up = await r.json()
        if not report.check("A ingests the filing", r.status == 200,
                            f"{up.get('chunks_ingested')} chunks"):
            return report.finish()

    answer, errors = await ask(args.b, token, "What were total revenues?",
                               dossier, headers, args.timeout)
    stranded = "No filing is attached to this dossier yet" in answer
    report.check("B answers from the filing A ingested",
                 bool(answer) and not stranded and not errors,
                 "B has its own private store — set CHROMA_HOST" if stranded
                 else f"{len(answer)} chars")

    report.section("Break #5 — two writers, one dossier")

    both = await asyncio.gather(
        ask(args.a, token, "Summarise the balance sheet.", dossier, headers, args.timeout),
        ask(args.b, token, "What are the main risk factors?", dossier, headers, args.timeout),
    )
    report.check("both instances answered",
                 all(text and not errs for text, errs in both),
                 " | ".join(f"{len(t)} chars" for t, _ in both))

    async with aiohttp.ClientSession(headers={**headers, **auth}) as http:
        rows: list[dict] = []
        for _ in range(40):
            async with http.get(
                f"{args.a}/api/conversations/{dossier}/messages?limit=100"
            ) as r:
                rows = (await r.json()).get("messages", [])
            if len([m for m in rows if m["role"] == "assistant"]) >= 3:
                break
            await asyncio.sleep(0.5)

        questions = [m for m in rows if m["role"] == "user"]
        answers = [m for m in rows if m["role"] == "assistant"]
        seqs = [m["seq"] for m in rows]
        report.check("every question reached the ledger", len(questions) == 3,
                     f"{len(questions)}/3")
        report.check("every answer reached the ledger", len(answers) == 3,
                     f"{len(answers)}/3 — a missing one is the seq race")
        report.check("positions are unique and contiguous",
                     seqs == list(range(1, len(rows) + 1)), str(seqs))

        async with http.delete(f"{args.a}/api/conversations/{dossier}") as r:
            await r.read()

    report.note(f"account {email} is left behind — it has no dossiers")
    return report.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--a", required=True, help="the first instance")
    parser.add_argument("--b", required=True, help="the second, a different one")
    parser.add_argument("--origin", default="http://localhost:8080")
    parser.add_argument("--filing", default=str(DEFAULT_FILING))
    parser.add_argument("--timeout", type=float, default=420.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
