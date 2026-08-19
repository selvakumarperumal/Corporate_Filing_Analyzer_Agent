# Corporate Filing Analyzer Agent (CFA Agent)

AI-powered corporate filing analysis assistant built with **LangGraph**, **FastAPI**, **Socket.IO**, **LangChain Ollama** (`llama3.1:latest` & `nomic-embed-text:latest`), **ChromaDB**, and a modern web frontend.

---

## Architecture

```
                       ┌────────────────────────────┐
                       │   Frontend (HTML/CSS/JS)   │
                       │  attach + ask · pick node  │
                       └─────────────┬──────────────┘
                                     │
                   WebSocket / Socket.IO  +  HTTP Upload
                                     │
                                     ▼
                       ┌────────────────────────────┐
                       │    FastAPI + Socket.IO     │
                       └─────────────┬──────────────┘
                                     │
                       ┌─────────────▼──────────────┐
                       │   LangGraph filing graph   │
                       │  (checkpointed per run)    │
                       └────────────────────────────┘
```

### The graph

```
                          ┌──────────────► financials ──┐
                          │               ► compliance ─┤
    START ─► retrieve ────┤  (manual)     ► risks ──────┼─► END
                          │               ► … 5 more ───┤
                          └─► router ─► approval ───────┘
                                            ▲
                                    interrupt() pause
```

| Node | Role |
| :--- | :--- |
| `retrieve` | Session-scoped Chroma similarity search; runs once per turn |
| `router` | LLM classification into one of 8 categories |
| `approval` | `interrupt()` — pauses the run for human confirmation or override |
| 8 category nodes | Category-specific prompt + streamed analysis |

**Routing is graph-level, not imperative.** Conditional edges decide the path:

- **Pick a node in the UI** → the edge out of `retrieve` jumps straight to that
  analysis node and the `router` never runs.
- **Leave it on Auto-route** → `router` classifies, then either analyses directly
  or (with approval on) stops at `approval`.

**Human-in-the-loop.** `approval` calls LangGraph's `interrupt()`, which suspends the
run and checkpoints the state. The UI renders the proposed route with *Approve* /
*Choose another node* / *Cancel*. Resuming sends `Command(resume={"category": …})`
on the same `thread_id`, so execution continues **at the pause** — retrieval and
classification are not repeated.

The checkpointer is `InMemorySaver`; swap it in
[`graph/builder.py`](backend/Analyzer/graph/builder.py) for a SQLite/Postgres
saver to survive restarts.

---

## Directory Structure

```
Corporate_Filing_Analyzer_Agent/
├── backend/
│   ├── config/
│   │   ├── logging.yaml
│   │   └── prompts.yaml
│   └── Analyzer/
│       ├── core/               # App configuration & logging
│       ├── models/             # Pydantic schemas
│       ├── prompts/            # LangChain Prompt templates
│       ├── graph/              # LangGraph workflow
│       │   ├── state.py        #   FilingState + category registry
│       │   ├── nodes.py        #   retrieve / router / approval / analysis
│       │   └── builder.py      #   conditional edges + checkpointer
│       ├── services/           # LLM, Vector, Router, Analysis, Chat
│       ├── api/                # Socket.IO event handlers
│       └── main.py             # FastAPI + Socket.IO entry point
└── frontend/                   # Standalone Web Client
    ├── index.html
    ├── style.css
    └── app.js
```

---

## Prerequisites

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

---

## Running the Application

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

Each is both an LLM router target and a direct entry point selectable in the UI.

---

## Using the workbench

The UI is a three-pane workbench: **dock** (filings + direct node entry) ·
**ledger** (numbered runs) · **inspector** (live graph map, retrieved context,
session readout).

- **Attach and ask together.** Drop files anywhere (or use 📎 / the dock
  dropzone) — they stage in the command bar and are vectorised and pushed to
  Chroma with the `session_id` the moment you hit RUN, so the same run retrieves
  from them. Retrieval is scoped to that dossier's filings, falling back to the
  whole collection when it has none.
- **Route** control in the command bar: `auto` hands the question to the router
  node; picking any category skips it. The dock's *Direct to node* list does the
  same in one click (`⌘K` / `Ctrl+K` opens the picker).
- **Approval gate** (top bar): holds auto-routed runs at the `approval` node.
  Not applicable to a manually picked node — you already made the decision.
- Every run carries a **trace** down its left edge (`retrieve → router →
  approval → analysis`) that fills in live, and the inspector's graph map lights
  the same path — amber while held at the gate.

---

## API

### REST

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/health` | Model + status |
| `GET` | `/api/categories` | Category ids, labels, descriptions |
| `GET` | `/api/graph` | Mermaid source of the compiled graph |
| `POST` | `/api/upload` | Ingest a filing (`file`, optional `session_id`) |

### Socket.IO

**Client → server**

| Event | Payload |
| :--- | :--- |
| `query` | `{ query, category: "auto"\|<id>, require_approval, session_id }` |
| `resume` | `{ run_id, category }` |

**Server → client**

| Event | Payload |
| :--- | :--- |
| `run_started` | `{ run_id }` |
| `status` | `{ stage: retrieve\|route\|analyze, message }` |
| `sources` | `{ sources: [{ source, page, snippet }] }` |
| `route` | `{ category, label, source: manual\|auto\|human, final }` |
| `interrupt` | `{ run_id, proposed_category, options }` — run is paused |
| `token` | `{ content }` |
| `done` | `{ run_id, category }` |
| `error` | `{ message }` |