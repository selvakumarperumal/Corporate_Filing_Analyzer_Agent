# Corporate Filing Analyzer Agent (CFA Agent)

AI-powered corporate filing analysis assistant built with **LangGraph**, **FastAPI**, **Socket.IO**, **LangChain Ollama** (`llama3.1:latest` & `nomic-embed-text:latest`), **ChromaDB**, **SQLModel** (async) over **Postgres** for accounts and conversation history, an optional **Redis** read cache, and a modern web frontend.

Dossiers persist. Every question and answer is a row, every filing stays in its
dossier's collection, and signing in resumes the work rather than starting it
over — see [Conversation history](#conversation-history).

Confused about how one socket serves every conversation, what clicking "New
dossier" actually does, or where a follow-up's history comes from? Start with
**[docs/DOSSIER-FAQ.md](docs/DOSSIER-FAQ.md)** — plain answers, one question at
a time.

For a walkthrough of what the app actually does — what happens when you click
"New dossier", how history is retrieved and trimmed, how a run travels from the
browser to the graph and back — see **[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md)**.

The real-time layer has a guide of its own: **[docs/SOCKETIO.md](docs/SOCKETIO.md)**
covers Socket.IO from first principles through to production — what it is, how
it is mounted onto FastAPI, how the connection is authenticated, and what nginx
has to be told for a token stream to arrive smoothly.

And for a walk through the backend's real-time layer one operation at a time —
what the server actually does when the analyst clicks "New dossier", asks a
question, reopens an old dossier or signs out, with the Socket.IO handlers shown
in full — see **[docs/FRONTEND-SOCKETIO.md](docs/FRONTEND-SOCKETIO.md)**.

Planning to run more than one instance? **[docs/SCALING.md](docs/SCALING.md)**
walks through what breaks and in what order — the JWT secret, the embedded
vector store, sticky sessions, graceful shutdown, and a race on message
positions that was quietly losing answers. The application-side ones are fixed;
what remains is deployment, and **[deploy/minikube/](deploy/minikube/)** is that
deployment, running: two API replicas against one shared vector store, on a
cluster on your laptop.

For the deployment itself — one process for writing code, minikube for the real
shape, what to test in each, and how to break each part on purpose to prove the
tests would have caught it — see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**,
with the runnable checks in **[deploy/checks/](deploy/checks/)**.

The database has its own guide too:
**[docs/DB-OPERATIONS.md](docs/DB-OPERATIONS.md)** covers the schema and every
read and write the app makes — what each question stores, how history is read
back, what cascades on a delete, and which statement each operation issues.

---

## Architecture

```
                       ┌────────────────────────────┐
                       │   Frontend (HTML/CSS/JS)   │
                       │  sign in · attach + ask    │
                       └─────────────┬──────────────┘
                                     │
              Socket.IO + HTTP, both carrying a bearer token
                                     │
                                     ▼
                       ┌────────────────────────────┐
                       │    FastAPI + Socket.IO     │
                       │  ┌──────────────────────┐  │      ┌────────────────┐
                       │  │  auth (JWT + bcrypt) │──┼─────►│    accounts    │
                       │  └──────────┬───────────┘  │      │                │
                       │  ┌──────────▼───────────┐  │      │  conversations │
                       │  │   history service    │──┼─────►│    messages    │
                       │  │ ledger ┆ run context │  │      │   (Postgres)   │
                       │  └──────────┬───────────┘  │      └───────┬────────┘
                       └─────────────┼──────────────┘              │ hot tail
                                     │ user id                     ▼
                       ┌─────────────▼──────────────┐      ┌────────────────┐
                       │   LangGraph filing graph   │      │  Redis (cache, │
                       │   (routes every question)  │      │   optional)    │
                       └────────────────────────────┘      └────────────────┘
```

Postgres is the source of truth for everything that has been said. Redis, if it
is configured at all, holds a copy of each conversation's recent tail — lose it
and the only difference is that reads go to Postgres instead.

### The graph

```
                                         ┌─► financials ─┐
    START ─► retrieve ─┬─► router ───────┤─► compliance ─┤
                       │                 │─► risks ──────┼─► END
                       │                 │─► … 5 more ───┤
                       │                 └───────────────┘
                       └─► no_filing ────────────────────┘
```

| Node | Role |
| :--- | :--- |
| `retrieve` | Similarity search over *this dossier's* Chroma collection |
| `router` | LLM classification into one of 8 categories, and naming the dossier |
| 8 category nodes | Category-specific prompt + streamed analysis |
| `no_filing` | Asks for a filing when the dossier has none indexed |

**Routing is automatic and graph-level, not imperative.** `retrieve` pulls the
passages, `router` classifies the question, and a single conditional edge out of
`router` dispatches to the matching analysis node — there is no manual entry
point and no approval pause.

A dossier with nothing indexed skips the analysis entirely and takes the
`no_filing` branch. Running a report prompt on an empty context does not produce
"I don't know" — it produces an invented report, so the graph never gets there.

**Every dossier is named after the question that opened it.** The `router`
node names an unnamed dossier alongside classifying its first question — one
extra LLM call, made concurrently with the classification so it costs no added
latency — and emits the name as a `title` event. The name is carried in
`FilingState`, returned on `done`, and sent back with every later question, so
a dossier is named once and never renamed. The dock lists the session's
dossiers under those names; a dossier goes by its id until it has one.

**Every answer is tagged with its route.** The `router` node emits a `route`
event carrying the chosen category, and the UI stamps it on the run as a badge.
The `done` event repeats the category, so an answer can never render untagged.
That tag is the *only* piece of the pipeline the UI surfaces — see
[Using the workbench](#using-the-workbench).

**The graph is stateless; the conversation is not.** It compiles without a
checkpointer and every run gets a fresh `thread_id` — there is nothing to pause
on mid-run. What carries across turns is the conversation itself, and that is
kept in the message table rather than in graph state: the ledger has to be
paged, rendered and read back by a browser that has been closed since, none of
which a checkpoint blob does well. `HistoryService` assembles the slice a run
needs and passes it in as `summary` + `history` on `FilingState`. Add a
checkpointer in [`analysis/graph/builder.py`](backend/Analyzer/analysis/graph/builder.py) if you
reintroduce a human-in-the-loop interrupt.

---

## Directory Structure

```
Corporate_Filing_Analyzer_Agent/
├── docker-compose.yml          # Workbench + API + Postgres + Redis
├── .env.example                # Settings compose reads; copy to .env
├── docs/
│   ├── DOSSIER-FAQ.md          # Plain answers: one socket, many dossiers, ids, history, storage
│   ├── HOW-IT-WORKS.md         # Walkthrough of every flow, from sign-in to summarisation
│   ├── SOCKETIO.md             # Socket.IO from zero to production: ideas, this app, deployment
│   ├── FRONTEND-SOCKETIO.md    # What the backend does for each workbench operation, handler by handler
│   ├── DB-OPERATIONS.md        # The schema, and every database read and write the app makes
│   ├── SCALING.md              # Running more than one instance: what breaks, and the fix for each
│   └── DEPLOYMENT.md           # Deploying it locally or on Kubernetes, and testing both
├── deploy/
│   └── cnpg-cluster.yaml       # The same database as a CloudNativePG Cluster (Kubernetes)
├── backend/
│   ├── Dockerfile              # Two-stage uv build -> uvicorn, non-root
│   ├── .dockerignore
│   ├── config/
│   │   ├── logging.yaml        # Console + rotating file handlers, per-module levels
│   │   └── prompts.yaml        # One system prompt per category
│   ├── logs/                   # analyzer.log + errors.log (created at startup)
│   ├── data/
│   │   └── chroma_db/          # One vector collection per dossier, kept across restarts
│   └── Analyzer/
│       ├── .env.example        # Every setting, with what it is for
│       ├── main.py             # FastAPI + Socket.IO entry point — assembly only
│       ├── container.py        # The process-wide services, wired together once
│       ├── core/               # Cross-cutting basics. Imports nobody.
│       │   ├── config.py       #   Settings, read from env / .env
│       │   ├── paths.py        #   Every runtime path, resolved in one place
│       │   ├── logging.py      #   Loads logging.yaml
│       │   └── tokens.py       #   Token estimate stored with every message
│       ├── db/                 # Persistence plumbing. No tables of its own.
│       │   ├── engine.py       #   Async Postgres engine, session factory, init_db()
│       │   └── columns.py      #   Shared id / timestamp column helpers
│       ├── auth/               # Domain: who is asking
│       │   ├── models.py       #   UserBase → User, RefreshToken (table=True)
│       │   ├── schemas.py      #   Signup / login / refresh / token pair bodies
│       │   ├── security.py     #   bcrypt hashing, access/refresh token minting
│       │   ├── service.py      #   Signup, login, refresh rotation, logout
│       │   └── routes.py       #   /api/auth/*
│       ├── conversations/      # Domain: dossiers and everything said in them
│       │   ├── models.py       #   Conversation, Message (jsonb metadata)
│       │   ├── schemas.py      #   Dossier + message page bodies
│       │   ├── cache.py        #   Optional Redis tail cache (off without REDIS_URL)
│       │   ├── service.py      #   The ledger, and the slice a run is sent
│       │   └── routes.py       #   /api/conversations/*
│       ├── analysis/           # Domain: reading filings and answering about them
│       │   ├── categories.py   #   The eight categories, named once
│       │   ├── prompts.py      #   Loads prompts.yaml into ChatPromptTemplates
│       │   ├── pipeline.py     #   AnalysisPipeline — the object everything talks to
│       │   ├── routes.py       #   /api/upload
│       │   ├── llm/            #   client / routing / analyst / summarizer
│       │   ├── retrieval/      #   vector_store.py — one collection per dossier
│       │   └── graph/          #   state.py, nodes.py, builder.py
│       └── api/                # Transport only. No rules of its own.
│           ├── dependencies.py #   current_user, DB session, service providers
│           ├── routes.py       #   Assembles the domain routers + /api/health
│           └── socket.py       #   connect / disconnect / query
└── frontend/                   # Standalone Web Client
    ├── Dockerfile              # nginx (unprivileged) serving static + proxying the API
    ├── nginx.conf              # /api + /socket.io -> backend, upload limit, ws upgrade
    ├── .dockerignore
    ├── index.html
    ├── style.css
    ├── config.js               # Where the client looks for the API (unset = localhost:8000)
    ├── config.docker.js        # Replaces config.js in the image (same-origin)
    ├── auth.js                 # Sign-in gate + token lifecycle
    └── app.js
```

---

## Prerequisites

> Running it with Docker instead? Only Ollama below is needed — the rest of the
> stack builds itself. Jump to [Running with Docker](#running-with-docker).

1. **Ollama** running locally with models:
   ```bash
   ollama pull llama3.1:latest
   ollama pull nomic-embed-text:latest
   ollama serve
   ```

2. **uv** (or Python >= 3.10):
   ```bash
   cd backend/Analyzer
   uv sync
   ```

3. **Configuration.** Copy the example and fill in a signing key:
   ```bash
   cd backend/Analyzer
   cp .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(64))"   # -> JWT_SECRET_KEY
   ```
   Everything has a working default except `JWT_SECRET_KEY`. Without one the
   app signs with a random key that dies with the process, which logs everyone
   out on every restart — fine while developing, and it says so in the log.

---

## Configuration

All settings are read from the environment or `backend/Analyzer/.env`; see
[`.env.example`](backend/Analyzer/.env.example) for the annotated list.

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `OLLAMA_MODEL` | `llama3.1:latest` | Analysis + routing model |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text:latest` | Embeddings for retrieval |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama is listening |
| `DATABASE_URL` | `postgresql+asyncpg://analyzer:analyzer@localhost:5432/filing_analyzer` | **Async Postgres URL.** Anything else is refused at startup |
| `DB_ECHO` | `false` | Log every SQL statement (debugging) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `10` | Connection pool, per API process |
| `DB_POOL_RECYCLE_SECONDS` | `1800` | Replace connections older than this rather than reuse them |
| `HISTORY_CONTEXT_MESSAGES` | `10` | Recent turns carried into a run |
| `HISTORY_CONTEXT_TOKENS` | `1500` | Ceiling on what those turns may cost |
| `HISTORY_SUMMARY_THRESHOLD` | `24` | Unsummarised turns tolerated before older ones are folded |
| `HISTORY_PAGE_SIZE` / `HISTORY_MAX_PAGE_SIZE` | `50` / `200` | Page size when a client reads the ledger back |
| `REDIS_URL` | *(blank)* | **Blank turns the cache off.** `redis://host:6379/0` to switch it on |
| `REDIS_KEY_PREFIX` | `cfa` | Key namespace, so one Redis can serve several apps |
| `REDIS_HOT_WINDOW` | `40` | Messages kept hot per conversation |
| `REDIS_TTL_SECONDS` | `3600` | How long an untouched conversation stays cached |
| `JWT_SECRET_KEY` | *(none)* | Signs both token kinds. **Set this.** |
| `JWT_ALGORITHM` | `HS256` | Signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `14` | Refresh token lifetime |
| `CORS_ORIGINS` | `["*"]` | Allowed browser origins, comma-separated (`http://a,http://b`) or a JSON list |

### Postgres, and only Postgres

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/filing_analyzer
```

Note the `+asyncpg`. Every query in the app is awaited, so a bare
`postgresql://` URL selects the synchronous driver — which is why the setting
is validated at startup and refused there, rather than raising on the first
request. `docker compose up` sets the URL for you from the `POSTGRES_*` values
in the root `.env`; `deploy/cnpg-cluster.yaml` is the same database as a
CloudNativePG `Cluster` for Kubernetes.

The schema leans on the server: message metadata is `jsonb`, timestamps are
`timestamptz`, and the `ON DELETE CASCADE` on `refresh_tokens.user_id` and
`messages.conversation_id` is enforced by Postgres without being asked. There
is no embedded-database fallback, and nothing in the code branches on which
store it is talking to.

Every query is async end to end — `sqlmodel.ext.asyncio.session.AsyncSession`
over an async engine, so the database never blocks the event loop the token
stream is running on.

Tables are created at startup by `init_db()`. That is enough for a single
service; put Alembic in front of it if you need versioned migrations.

### One class, both jobs

The account tables are SQLModel, so a declaration is at once the Pydantic model
and the SQLAlchemy table. The shared fields live on `UserBase`, and both halves
inherit it:

```
                  UserBase  (email: EmailStr, name: str)
                 /        \
    User(table=True)      SignupRequest   ── validated (+ password)
    ── the row             UserOut        ── returned (+ id, created_at)
```

The email and name rules are written once and the column definition, the
request body and the response body all come from them.

SQLModel **skips validation on `table=True` classes** — `User(email="nonsense")`
builds a row without complaint. `User.model_validate()` does *not* skip it, so
`signup` builds rows that way and the `UserBase` rules hold for anything that
reaches the table, not only for what arrived through a request body. The rules
live on `UserBase` for the same reason: `min_length=1` alone would accept a name
of three spaces, so the strip-and-refuse-blank check sits beside it rather than
on the request schema.

There are no ORM relationships between the two tables. Nothing traverses from a
user to their tokens — the queries go straight at `refresh_tokens` — and an
unused relationship is a liability under async, where reading an unloaded one
raises `MissingGreenlet` from wherever it was touched. The foreign key carries
`ON DELETE CASCADE` instead.

> Deleting an account therefore takes its refresh tokens, its dossiers and
> every message in them with it, in one statement, decided by Postgres rather
> than by application code that could be skipped.

---

## Conversation history

Everything said in a dossier is a row in `messages`, keyed to a row in
`conversations`. Rows rather than one JSON blob per dossier: a blob is fine
until something needs to page through a conversation, edit a single message or
count tokens across a range — and then it is a rewrite.

```
conversations                          messages
  id            ours                     id
  user_id  ───► users.id (CASCADE)       conversation_id ──► conversations.id (CASCADE)
  client_id     the browser's id         seq              position, from 1
  title         named by the router      role             user | assistant | system
  filings       jsonb register           content
  summary       rolling, see below       tokens           estimated at write time
  summary_through_seq                    status           complete | error
  message_count                          meta             jsonb (jsonb on Postgres)
  last_message_at                        created_at
```

**Two ids name a conversation, and the difference matters.** `client_id` is the
dossier id the browser minted; it is unique *per account*, never on its own, so
nothing is ever looked up by it without an owner beside it. An id from one
account can not resolve to another's dossier.

**`meta` is `jsonb` on Postgres and plain JSON elsewhere.** What hangs off a
message varies by what produced it — attached filings, the run that answered,
the category, the reason a run failed — and none of it is queried structurally,
so it lives there instead of as a column each new kind of message would add.

**Ordering is `seq`, not `created_at`.** Two messages written in the same
millisecond are a coin toss by timestamp, and pagination needs a total order.
`(conversation_id, seq)` is unique, which is also what stops two writers
claiming the same position.

### Display history is not context history

| | Display history | Context history |
| :--- | :--- | :--- |
| Who reads it | The analyst, in the ledger | The model, on every run |
| Contents | Everything, untrimmed | Recent turns + a rolling summary |
| Shape | Paged, oldest first | Bounded by messages *and* tokens |
| Where | `GET /api/conversations/{id}/messages` | `HistoryService.context_for()` |

Keeping them apart is the point of
[`conversations/service.py`](backend/Analyzer/conversations/service.py).
The record of what was asked and answered should not change shape as a dossier
gets long; the prompt has no choice but to. Four passes narrow the second one:

1. anything already folded into the summary is dropped;
2. so are runs that failed — a question the analyzer never answered is kept in
   the ledger for the analyst to see, but conditioning the next answer on an
   error message only teaches the model to apologise;
3. all but the last `HISTORY_CONTEXT_MESSAGES` turns;
4. whatever does not fit `HISTORY_CONTEXT_TOKENS`, oldest first.

That last pass is why **every message is stored with a token count**. Managing a
context window after the fact is guesswork without one, and write time is the
only moment the text is already in hand. It is an estimate, not a tokenizer's
answer — Ollama does not expose one as a library, and a real count would mean a
round trip per message — so [`core/tokens.py`](backend/Analyzer/core/tokens.py)
estimates high, on the reasoning that a history trimmed slightly too hard still
answers while one trimmed too softly overflows and fails.

### The rolling summary

Past `HISTORY_SUMMARY_THRESHOLD` unsummarised turns, everything but the recent
tail is folded into `conversations.summary` by
[`analysis/llm/summarizer.py`](backend/Analyzer/analysis/llm/summarizer.py) —
the same model that answers, given the previous summary and only what has
happened since, so the cost of summarising does not grow with the conversation.

It runs **after** an answer is delivered, never before the next one is asked
for: it is another LLM call, and no analyst should wait on last week's history
being compressed. A fold that fails leaves the conversation with a longer
unsummarised tail and is retried after the next run — a bad summary would
quietly distort every answer after it, so not summarising is the safer failure.

### Redis is a cache, not a store

`REDIS_URL` blank is a supported configuration and the default: every read goes
to the database, which is correct, just slower. With it set,
[`conversations/cache.py`](backend/Analyzer/conversations/cache.py) keeps each conversation's last
`REDIS_HOT_WINDOW` messages under a TTL that every read pushes out again, so
active dossiers stay hot and abandoned ones fall out on their own.

Every operation fails soft. A missing package, an unreachable server or an error
mid-request all disable the cache and log it rather than raising into the run —
and because nothing is stored only in Redis, flushing it loses nothing.

### Filings outlive the process

Collections used to be cleared wholesale at startup, which was right when a
dossier lasted only as long as the browser tab. Now that a dossier is a row, its
filings have to survive with it: `VectorService.prune_to()` runs at startup
against the conversations still in the database and drops only what nothing
claims any more — collections whose dossier was deleted while the backend was
down, or left behind by a crash between ingesting a file and recording it.

Deleting a dossier deletes both halves, in that order. A conversation whose
messages were kept while its filings were dropped would answer follow-ups out of
a summary of documents it can no longer cite.

---

## Running with Docker

The whole stack — workbench, API, Postgres and Redis — comes up with one
command. The model does not: Ollama stays on the host, because it wants the
GPU and a multi-gigabyte model directory that has no business inside a
container image.

| Service | Image | Published | What it is |
| :--- | :--- | :--- | :--- |
| `frontend` | built from [`frontend/Dockerfile`](frontend/Dockerfile) | **8080** | nginx serving the client, and reverse-proxying `/api` + `/socket.io` to the API |
| `backend` | built from [`backend/Dockerfile`](backend/Dockerfile) | — | FastAPI + Socket.IO under uvicorn |
| `postgres` | `postgres:17-bookworm` | — | Accounts and the message ledger |
| `redis` | `redis:7-alpine` | — | The conversation read cache |

Only the workbench is published to the host. Everything else talks over the
compose network, so there is no database or API port exposed on your machine.

### 1. Ollama, on the host

```bash
ollama pull llama3.1:latest
ollama pull nomic-embed-text:latest
```

Ollama binds loopback by default, which a container cannot reach. Restart it
listening more widely:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

The API reaches it at `host.docker.internal:11434`. That name is provided
automatically on Docker Desktop; on Linux the compose file maps it to the host
gateway explicitly, so it works there too.

### 2. Settings

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # -> JWT_SECRET_KEY
```

Every value has a working default, so the stack starts without this step — but
without `JWT_SECRET_KEY` the API signs tokens with a random key that dies with
the process, logging everyone out on each restart.

### 3. Up

```bash
docker compose up --build
```

Then open **[http://localhost:8080](http://localhost:8080)** and create an
account. The first build takes a few minutes; afterwards the dependency layer
is cached and rebuilds are quick.

The API is *not* published separately — it is reached through the workbench's
own origin, at `http://localhost:8080/api/…`, with `/docs` serving the OpenAPI
explorer. That is deliberate: one origin means the browser never makes a
cross-origin request, so CORS, credentials and the Socket.IO handshake all stop
being something to configure.

### Everyday commands

```bash
docker compose up -d --build          # start in the background
docker compose logs -f backend        # follow the API log
docker compose ps                     # what is running, and health
docker compose restart backend        # pick up an env change
docker compose down                   # stop, keeping all data
docker compose down -v                # stop and delete accounts, ledger, filings
```

### What is stored where

Three named volumes outlive `docker compose down`:

| Volume | Holds | Lost if dropped |
| :--- | :--- | :--- |
| `postgres-data` | Accounts, dossiers, every message | Sign-ins and all history |
| `filing-data` | The Chroma collections — the filings themselves | Dossiers survive with nothing left to answer from |
| `filing-logs` | `analyzer.log`, `errors.log` | Only the logs |

`docker compose down -v` drops all three. Redis is intentionally not in that
list: it is a cache, nothing lives only there, and flushing it costs one slower
read per conversation.

### A note on CloudNativePG

CNPG is a Kubernetes **operator**, not an image you can run under compose. Its
operand image (`ghcr.io/cloudnative-pg/postgresql`) ships no
`docker-entrypoint.sh` — its `CMD` is a bare `bash`, and it is the operator's
instance manager that runs `initdb` and starts Postgres. Given
`POSTGRES_PASSWORD` and nothing else it simply exits.

So compose runs the official `postgres:17-bookworm` image, on the same major
version CNPG would give you, and [`deploy/cnpg-cluster.yaml`](deploy/cnpg-cluster.yaml)
is the same database as a real CNPG `Cluster` for Kubernetes — a three-instance,
replicated cluster on the CNPG operand image. Applying it is the two commands in
the header of that file. The app needs no code change either way; CNPG hands you
a connection URI in a generated secret, and only its scheme has to be rewritten
from `postgresql://` to `postgresql+asyncpg://`.

### When it does not come up

**The workbench loads but every question fails.** Almost always Ollama. The
backend log names it:

```bash
docker compose logs backend | grep -i ollama
curl http://localhost:11434/api/tags        # is it up on the host at all?
```

If that responds but the container disagrees, Ollama is bound to loopback —
restart it with `OLLAMA_HOST=0.0.0.0`.

**`backend` restarts in a loop.** Check the database URL and the driver:

```bash
docker compose logs backend | tail -40
```

A `postgresql://` URL without `+asyncpg` fails here; so does changing
`POSTGRES_USER`/`POSTGRES_PASSWORD` in `.env` after the first run, because the
credentials were baked into the volume on its first start. Reconcile them, or
`docker compose down -v` and start over.

**Uploads fail on a large PDF.** nginx accepts 64 MB
([`frontend/nginx.conf`](frontend/nginx.conf), `client_max_body_size`); raise it
there and `docker compose up -d --build frontend` if your filings are bigger.

---

## Running without Docker


### 1. Start Backend Server
```bash
cd backend/Analyzer
uv run uvicorn main:asgi_app --host 0.0.0.0 --port 8000 --reload
```

### 2. Serve the Frontend
```bash
cd frontend
python3 -m http.server 3000
```
Open [http://localhost:3000](http://localhost:3000) — it auto-connects to the
backend on port `8000`. (`http://localhost:8000` itself returns the API status
JSON; `/docs` has the OpenAPI explorer.)

---

## Supported Filing Analysis Categories

1. **Financial Highlights (`financials`)**: Revenue, net income, EBITDA, margins, debt, cash flow, YoY growth.
2. **Compliance & Audit (`compliance`)**: Auditor opinion (Qualified/Clean), Key Audit Matters, SOX 404, regulatory issues.
3. **Item 1A Risk Factors (`risks`)**: Categorized operational, market, cybersecurity, credit, and legal risks with severity ratings.
4. **Shareholding Pattern (`shareholding`)**: Promoter, institutional, and retail ownership percentages.
5. **Corporate Governance (`governance`)**: Board composition, executive compensation, and related-party disclosures.
6. **MD&A Outlook (`mda`)**: Management Discussion and Analysis summary and strategic initiatives.
7. **Executive Summary (`summary`)**: Comprehensive filing overview and top takeaways.
8. **General Q&A (`qa`)**: Factual question answering on corporate filing text.

The router picks between all 8 on its own; none is selectable in the UI.

---

## Accounts & sessions

The workbench is behind a sign-in gate. Filings are private to the account that
uploaded them, and everything past `/api/auth` and `/api/health` needs a bearer
token — including the Socket.IO handshake, which is refused outright without
one.

### Two tokens

| | Lifetime | Sent | Stored server-side |
| :--- | :--- | :--- | :--- |
| **Access** | 15 min | On every request, and in the socket handshake | No |
| **Refresh** | 14 days | Only to `/api/auth/refresh` | Yes, by `jti`, so it can be revoked |

Access tokens are deliberately not checked against the database — that is what
makes them cheap. The cost is that revoking a session takes effect on the access
token's own expiry, which is why it is short.

**Refresh tokens rotate.** Spending one revokes it and issues a new pair, so the
token in the browser is only ever the newest. A token that comes back a second
time is either a replay or a stolen copy racing the real client, and the two
cannot be told apart — so presenting an already-spent token revokes *every*
session that user has. The thief is locked out; the owner signs in again.

### What the browser does

[`frontend/auth.js`](frontend/auth.js) keeps the pair in `localStorage` and
holds the session open without the analyst noticing:

- a timer refreshes ~1 min before the access token expires, so requests rarely
  meet a 401 at all;
- `authFetch` refreshes once and retries on a 401 anyway, so an upload that
  straddled an expiry still lands;
- concurrent refreshes share one in-flight request — spending the refresh token
  three times over would rotate it out from under itself;
- the socket's `auth` is a *callback*, so a reconnection an hour later hands
  over the token current at that moment, not the one it first opened with;
- a handshake refused for a bad token refreshes and reconnects, rather than
  retrying the same dead credential.

Signing out clears the workbench but discards nothing: the dossiers are the
account's, not the browser's, and signing in again — here or anywhere else —
brings them back with their runs and their filings. Only an explicit **bin**
throws work away.

### Scoping

A dossier id is generated by the browser, so on its own it is only as
unguessable as the browser made it. The backend namespaces it under the account
(`user_id:session_id`) before it ever reaches Chroma, so there is no id a
signed-in user can send that resolves to someone else's uploads. The browser
only ever sees its own id back.

Passwords are bcrypt-hashed (and rejected past bcrypt's 72-byte limit rather
than silently truncated). A login for an unknown address runs a dummy hash
comparison and returns the same message as a wrong password, so neither the
response nor its timing reveals which addresses are registered.

---

## Using the workbench

Two panes: **dock** (session actions and a read-only register of the filings in
this dossier) and **ledger** (numbered runs, each one a question and its
answer). The pipeline behind an answer is deliberately not on screen — no graph
map, no node trace, no connection or model readout. What the analyst needs is
the answer and what kind of analysis produced it.

- **Attach and ask together.** **Attach filing** sits in the command bar,
  directly under the question it ships with (or drop files anywhere on the
  page). Staged filings show as chips above the input and are pushed to this
  dossier's collection the moment you hit RUN, so the same run retrieves from
  them.
- **One dossier, one corpus.** Each dossier reads only the filings attached to
  it. **New dossier** opens an empty one alongside the others; the **bin**
  discards the current dossier outright — its runs and its filings, on the
  backend as well as on screen. Both live at the top of the dock, next to the
  filings they act on.
- **Dossiers come back.** Signing in lists the account's dossiers newest first
  and puts the most recent on the stage. A dossier's runs are fetched the first
  time it is opened, newest page first, with **load earlier runs** at the top of
  anything longer than a page — an analyst with forty dossiers should not wait
  for the thirty-nine they are not about to read.
- **A run belongs to one dossier for its whole life.** It uploads into, asks of,
  and renders into the dossier it was opened in. Every event the backend streams
  back is stamped with that dossier's id, and the client drops anything stamped
  for a dossier it has moved on from — so a run cut short by a dropped
  connection can never write into the dossier now on screen.
- **No route control.** The router reads every question and picks the node, and
  there is nothing in the UI to override it.
- **Every answer is tagged** with the kind of analysis behind it — a
  tone-coloured `COMPLIANCE` badge in the run bar. It is set from the `route`
  event and re-applied on `done`, so an answer is never untagged.
- While a run is in flight the status line reads in plain language ("reading the
  filing", "writing the compliance answer"), never in node names.
- Connection trouble surfaces as a toast when it happens rather than as a
  permanent status indicator.

---

## API

### REST

Everything marked 🔒 needs `Authorization: Bearer <access_token>`.

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/health` | Model + status. Open — a health check that needs a login cannot tell you whether logins work |
| `POST` | `/api/auth/signup` | `{ email, name, password }` → `201` + token pair. `409` if the address is taken, `422` if a detail fails validation |
| `POST` | `/api/auth/login` | `{ email, password }` → token pair. `401` on bad credentials *or* an unknown address, same message either way |
| `POST` | `/api/auth/refresh` | `{ refresh_token }` → a new pair; the one presented is revoked |
| `POST` | `/api/auth/logout` | `{ refresh_token }` → `{ status }`. Always `200` — the token is unusable afterwards regardless |
| `GET` | `/api/auth/me` | 🔒 The signed-in analyst |
| `POST` | `/api/upload` | 🔒 Ingest a filing into one dossier (`file`, `session_id` — both required); opens the dossier if it is new |
| `GET` | `/api/conversations` | 🔒 The analyst's dossiers, most recently spoken in first — name, run count, filings |
| `GET` | `/api/conversations/{id}/messages` | 🔒 One page of the ledger, oldest first. `?limit=&before_seq=` — page backwards with the previous response's `next_before_seq` |
| `PATCH` | `/api/conversations/{id}` | 🔒 `{ title }` — rename a dossier by hand |
| `DELETE` | `/api/conversations/{id}` | 🔒 Discard a dossier: its messages *and* the collection its filings live in |

A token pair is:

```json
{
  "access_token": "eyJ…", "refresh_token": "eyJ…", "token_type": "bearer",
  "expires_in": 900,
  "user": { "id": "…", "email": "…", "name": "…", "created_at": "…" }
}
```

`expires_in` is the access token's lifetime in seconds, so a client can refresh
ahead of expiry instead of waiting for a 401.

### Socket.IO

**Handshake** — `auth: { token }` carrying the access token (or an
`Authorization: Bearer` header for non-browser clients). A handshake without a
valid one is refused, so the client sees `connect_error` and knows to refresh
and retry rather than sitting on a connection that would reject every query.

```js
const socket = io(BACKEND_URL, { auth: (cb) => cb({ token: Auth.accessToken }) });
```

**Client → server**

| Event | Payload |
| :--- | :--- |
| `query` | `{ query, session_id, title, files }` — `session_id` required; a query without one is refused. `title` is the dossier's name, blank if it has none yet. `files` are the names of the filings attached to this question, recorded with it |

**Server → client** — every payload also carries the `session_id` it was
produced for, so a client can drop events meant for a dossier it has closed.

| Event | Payload |
| :--- | :--- |
| `run_started` | `{ run_id, session_id }` |
| `status` | `{ stage: retrieve\|route\|analyze, category, session_id }` — `category` on `analyze` only |
| `route` | `{ category, session_id }` — the answer's tag |
| `title` | `{ title, session_id }` — the dossier's name, on its first answered question only |
| `token` | `{ content, session_id }` |
| `done` | `{ run_id, category, title, session_id }` |
| `error` | `{ message, session_id }` |