# Analyzer

The backend package for the Corporate Filing Analyzer Agent: a FastAPI + Socket.IO
app that answers questions about corporate filings, one isolated dossier at a time.

Run it from this directory:

```bash
uv sync
cp .env.example .env          # then set JWT_SECRET_KEY and DATABASE_URL
uv run uvicorn main:asgi_app --reload
```

`asgi_app`, not `app`: it is the Socket.IO server wrapping the FastAPI app, and
mounting `main:app` instead serves the REST API but drops every websocket.

The full setup — Ollama models, Postgres, Redis, Docker — is in the
[repository README](../../README.md); how a request actually flows through this
package is in [docs/HOW-IT-WORKS.md](../../docs/HOW-IT-WORKS.md).

## Layout

Organised by domain, not by kind: a feature is a folder, and everything that
feature needs — its tables, its schemas, its service, its routes — is in it.

| Package | What lives there |
| :--- | :--- |
| `core/` | Settings, paths, logging, token estimation. Imports nothing else in the app. |
| `db/` | The async Postgres engine, the session factory, shared column helpers. No tables of its own. |
| `auth/` | Who is asking: `User`/`RefreshToken`, bcrypt + JWT, signup/login/refresh, `/api/auth/*`. |
| `conversations/` | Dossiers and everything said in them: the ledger, the Redis tail cache, `/api/conversations/*`. |
| `analysis/` | Reading filings and answering about them: categories, prompts, `llm/`, `retrieval/`, `graph/`, `/api/upload`. |
| `api/` | Transport only: dependency providers, the assembled router, the Socket.IO handlers. |
| `container.py` | The composition root — the process-wide services, wired together once. |
| `main.py` | Assembly: the app, its middleware, the lifespan, both protocols mounted side by side. |

The import direction is one-way — `core` ← `db` ← domains ← `api` ← `main` — so
a domain never has to know what is mounted in front of it.

## Storage

Three stores, and they are not interchangeable:

- **Postgres** is the source of truth for accounts, dossiers and messages.
  `DATABASE_URL` must be an async Postgres URL (`postgresql+asyncpg://…`);
  anything else is refused at startup rather than failing on the first query.
- **Chroma** (`../data/chroma_db/`) holds the filing text and its embeddings,
  one collection per dossier, keyed by the owner as well as the dossier id.
- **Redis** is optional and holds nothing of its own — a read cache of each
  conversation's hot tail. Leave `REDIS_URL` blank and the cache is simply off.
