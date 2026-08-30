#!/usr/bin/env python3
"""Does this deployment work at all? One endpoint, the whole analyst path.

    python deploy/checks/smoke.py --base http://cfa.local

Signs up, attaches a filing, asks about it, reads the ledger back, and throws
the dossier away — the same sequence a browser performs, over the same two
protocols. Everything else in this directory assumes this one passes.

Needs the backend's environment for `aiohttp` and `python-socketio`:

    backend/Analyzer/.venv/bin/python deploy/checks/smoke.py --base …
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
except ImportError as error:  # pragma: no cover - an environment problem
    die(f"{error.name} is missing — run this with backend/Analyzer/.venv/bin/python")

REPO = Path(__file__).resolve().parents[2]
DEFAULT_FILING = REPO / "mock_10k_filing.txt"


async def run(args: argparse.Namespace) -> int:
    report = Report(f"Smoke test — {args.base}")
    headers = {"Origin": args.origin}
    if args.host:
        headers["Host"] = args.host

    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    dossier = uuid.uuid4().hex
    token = ""

    async with aiohttp.ClientSession(headers=headers) as http:
        report.section("HTTP")

        try:
            async with http.get(
                f"{args.base}/api/health", timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                status, body = r.status, (await r.json() if r.status == 200 else {})
        except Exception as error:
            report.check("/api/health answers without a token", False,
                         f"{type(error).__name__}: {error}")
            report.note(f"nothing is listening at {args.base}")
            return report.finish()
        report.check("/api/health answers without a token", status == 200, str(body))

        async with http.post(
            f"{args.base}/api/auth/signup",
            json={"email": email, "password": "Sup3rSecret!23", "name": "Smoke"},
        ) as r:
            body = await r.json()
        if not report.check("signup returns a token pair", r.status == 201, str(body)[:120]):
            return report.finish()
        token = body["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        async with http.get(f"{args.base}/api/auth/me", headers=auth) as r:
            me = await r.json()
        report.check("the token identifies the account", r.status == 200 and me.get("email") == email)

        filing = Path(args.filing)
        if not filing.is_file():
            die(f"no filing to upload at {filing}")
        form = aiohttp.FormData()
        form.add_field("session_id", dossier)
        form.add_field("file", filing.read_bytes(), filename=filing.name,
                       content_type="text/plain")
        async with http.post(f"{args.base}/api/upload", data=form, headers=auth) as r:
            up = await r.json()
        chunks = up.get("chunks_ingested", 0) if r.status == 200 else 0
        if not report.check("the filing is ingested", r.status == 200 and chunks > 0,
                            f"{chunks} chunks" if chunks else str(up)[:120]):
            return report.finish()

        async with http.get(f"{args.base}/api/conversations", headers=auth) as r:
            dossiers = await r.json()
        report.check("attaching a filing opens the dossier",
                     any(d["id"] == dossier for d in dossiers))

    report.section("Socket.IO")

    sio = socketio.AsyncClient()
    answer: list[str] = []
    stages: list[str] = []
    finished = asyncio.Event()
    errors: list[str] = []

    @sio.on("token")
    async def _token(data): answer.append(data.get("content", ""))
    @sio.on("status")
    async def _status(data): stages.append(data.get("stage", ""))
    @sio.on("done")
    async def _done(data): finished.set()
    @sio.on("error")
    async def _error(data): errors.append(data.get("message", "")); finished.set()

    try:
        await sio.connect(args.base, auth={"token": token}, headers=headers,
                          transports=["websocket"], wait_timeout=20)
    except Exception as error:
        report.check("the handshake is accepted", False, str(error)[:140])
        report.note("a 400 here usually means this origin is not in CORS_ORIGINS")
        return report.finish()
    report.check("the handshake is accepted", True, f"sid={sio.sid}")

    await sio.emit("query", {"query": args.question, "session_id": dossier,
                             "title": "", "files": [Path(args.filing).name]})
    try:
        await asyncio.wait_for(finished.wait(), timeout=args.timeout)
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
    await sio.disconnect()

    text = "".join(answer)
    report.check("the answer streams back", not timed_out and bool(text),
                 f"{len(text)} chars" if text else f"timed out after {args.timeout}s")
    report.check("no error event", not errors, "; ".join(errors)[:140] if errors else "")
    report.check("the answer is drawn from the filing",
                 "No filing is attached to this dossier yet" not in text)

    report.section("The ledger")

    async with aiohttp.ClientSession(headers={**headers, "Authorization": f"Bearer {token}"}) as http:
        # `done` reaches the client before the answer row is written, so poll.
        rows: list[dict] = []
        for _ in range(30):
            async with http.get(
                f"{args.base}/api/conversations/{dossier}/messages?limit=50"
            ) as r:
                page = await r.json()
            rows = page.get("messages", [])
            if any(m["role"] == "assistant" for m in rows):
                break
            await asyncio.sleep(0.5)

        report.check("the question is recorded",
                     any(m["role"] == "user" for m in rows))
        report.check("the answer is recorded",
                     any(m["role"] == "assistant" and m["status"] == "complete" for m in rows),
                     f"{len(rows)} row(s)")
        report.check("positions are contiguous from 1",
                     [m["seq"] for m in rows] == list(range(1, len(rows) + 1)),
                     str([m["seq"] for m in rows]))

        async with http.delete(f"{args.base}/api/conversations/{dossier}") as r:
            gone = await r.json()
        report.check("the dossier and its filings can be discarded",
                     r.status == 200 and gone.get("deleted"), str(gone))

    report.note(f"account {email} is left behind — it has no dossiers")
    return report.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True,
                        help="where the app is, e.g. http://cfa.local")
    parser.add_argument("--origin", default="http://localhost:8080",
                        help="Origin header; must be in CORS_ORIGINS")
    parser.add_argument("--host", default="",
                        help="Host header, when reaching an ingress by IP")
    parser.add_argument("--filing", default=str(DEFAULT_FILING))
    parser.add_argument("--question",
                        default="What were total revenues and the main risk factors?")
    parser.add_argument("--timeout", type=float, default=420.0,
                        help="seconds to wait for an answer on a local model")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
