# Deploying it, and proving it works

Four ways to run this app, from a laptop with no containers to two replicas on
Kubernetes, and how to test each of them — including how to break each one on
purpose, so you know the test would have caught it.

The app is scalable, but only in the configuration that makes it so. Two
settings decide it, and everything in this file circles back to them:

```bash
JWT_SECRET_KEY=…      # the same value on every instance
CHROMA_HOST=chroma    # one vector store, not one per instance
```

Get either wrong with more than one instance and you get symptoms that look
like network problems and are not. [SCALING.md](SCALING.md) explains why;
this file is how you deploy it and how you check.

---

## Contents

1. [Which one do you want](#1-which-one-do-you-want)
2. [Before anything: the model](#2-before-anything-the-model)
3. [Case A — no containers](#3-case-a--no-containers)
4. [Case B — compose, one API](#4-case-b--compose-one-api)
5. [Case C — compose, two APIs](#5-case-c--compose-two-apis)
6. [Case D — minikube, the reference deployment](#6-case-d--minikube-the-reference-deployment)
7. [Case E — variations](#7-case-e--variations)
8. [Every setting that decides whether it scales](#8-every-setting-that-decides-whether-it-scales)
9. [The checks](#9-the-checks)
10. [What to test, per case](#10-what-to-test-per-case)
11. [Breaking it on purpose](#11-breaking-it-on-purpose)
12. [Reading the logs](#12-reading-the-logs)
13. [Symptom → cause](#13-symptom--cause)

---

## 1. Which one do you want

| | Case A | Case B | Case C | Case D |
| --- | --- | --- | --- | --- |
| | bare local | compose | compose ×2 | **minikube** |
| API instances | 1 | 1 | 2 | 2 |
| Vector store | embedded | embedded | **shared** | **shared** |
| Postgres | yours | container | container | StatefulSet |
| Redis | optional | container | container | Deployment |
| Sticky sessions | n/a | n/a | no | **yes, at the ingress** |
| Graceful shutdown | no | no | no | **yes** |
| Good for | writing code | using the app | **finding scaling bugs** | **the real thing, locally** |
| Start-up cost | seconds | a minute | a minute | ten minutes |

**Case D is the one that resembles production.** Case C is the cheapest way to
reproduce a multi-instance bug, and the cheapest way to *see* one: run it
without `CHROMA_HOST` and Break #2 shows up within a minute of ordinary use.

---

## 2. Before anything: the model

None of these run Ollama. It lives on your machine in every case except the
optional in-cluster variant ([Case E](#7-case-e--variations)).

```bash
ollama pull llama3.1:latest
ollama pull nomic-embed-text:latest
OLLAMA_HOST=0.0.0.0 ollama serve
```

That last line is not optional for B, C or D. Ollama binds loopback by default,
and a container reaching `host.docker.internal` or `host.minikube.internal` is
not loopback. The symptom if you skip it is a health check that passes and
every question that fails.

```bash
curl -s http://localhost:11434/api/tags | head -c 200      # from the host
```

---

## 3. Case A — no containers

For working on the code. One process, embedded vector store, and a Postgres you
provide.

```bash
# Postgres somewhere — a container is fine even here
docker run -d --name cfa-pg -p 5432:5432 \
  -e POSTGRES_USER=analyzer -e POSTGRES_PASSWORD=analyzer \
  -e POSTGRES_DB=filing_analyzer postgres:17-bookworm

cd backend/Analyzer
uv sync
uv run uvicorn main:asgi_app --reload --port 8000
```

and the client, from `frontend/`:

```bash
python3 -m http.server 5500
```

`frontend/config.js` leaves `__BACKEND_URL__` unset, so the page falls back to
`http://localhost:8000`. `CORS_ORIGINS` defaults to `*`, so the socket connects.

**Test it:**

```bash
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py --base http://localhost:8000
```

Nothing about scaling applies here — one process is allowed to keep the filings
on its own disk and sign with a throwaway key. The startup log says so:

```
WARNING  main  Tokens are signed with a key that dies with this process…
INFO     …vector_store  VectorService ready (store=…/backend/data/chroma_db, shared=False, …)
```

---

## 4. Case B — compose, one API

The everyday stack: workbench, API, Postgres, Redis.

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # paste into JWT_SECRET_KEY
docker compose up --build
```

→ **http://localhost:8080**

`CHROMA_HOST` stays blank. One API, one embedded store, and the filings live in
the `filing-data` volume. That is the correct configuration for one instance,
not a compromise.

**Test it:**

```bash
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py --base http://localhost:8080
```

Setting `JWT_SECRET_KEY` matters even here, for a reason that has nothing to do
with scaling: without it, `docker compose restart backend` signs everybody out.

---

## 5. Case C — compose, two APIs

The cheapest way to run the app the way a cluster runs it. Two API containers
behind one shared vector store:

```bash
CHROMA_HOST=chroma docker compose --profile scaled up --scale backend=2
```

The `scaled` profile is what starts Chroma; `CHROMA_HOST` is what makes the
backends use it. Both are needed, and forgetting the second is the interesting
mistake — see [§11](#11-breaking-it-on-purpose).

**Reaching the two containers separately.** `split.py` needs two different
endpoints, and compose does not publish scaled containers on distinct host
ports. Port-forward with `docker` instead:

```bash
docker compose ps --format '{{.Name}}' | grep backend
# corporate-filing-analyzer-backend-1, -2

docker run -d --rm --name fwd-a --network container:corporate-filing-analyzer-backend-1 \
  -p 18001:18001 alpine/socat tcp-listen:18001,fork,reuseaddr tcp:127.0.0.1:8000
docker run -d --rm --name fwd-b --network container:corporate-filing-analyzer-backend-2 \
  -p 18002:18002 alpine/socat tcp-listen:18002,fork,reuseaddr tcp:127.0.0.1:8000
```

Then:

```bash
backend/Analyzer/.venv/bin/python deploy/checks/split.py \
    --a http://127.0.0.1:18001 --b http://127.0.0.1:18002
```

**What compose does not give you:** sticky sessions, a graceful termination
budget, or an ordered rollout. Breaks #3 and #4 are not testable here. That is
what Case D is for.

---

## 6. Case D — minikube, the reference deployment

Two API replicas, one shared store, a signing key from a Secret, cookie
affinity on the socket path, and a shutdown long enough to finish an answer.

```bash
./deploy/minikube/bootstrap.sh
echo "$(minikube ip)  cfa.local" | sudo tee -a /etc/hosts
```

→ **http://cfa.local**

The script starts minikube if it is not running, enables the ingress addon,
builds both images into the cluster's own runtime, generates the JWT key,
applies the manifests and waits. Running it again is safe; the one thing it
will not redo is the signing key.

By hand, and what each step is for, is in
[`deploy/minikube/README.md`](../deploy/minikube/README.md).

**The first thing to check, before anything else:**

```bash
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 \
  | grep "VectorService ready"
```

Two lines, both `shared=True`. One `shared=False` among them is a deployment
that will lose filings, and every other check can still pass.

**Test it, in this order:**

```bash
# 1. does it work at all
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py \
    --base "http://$(minikube ip)" --host cfa.local --origin http://cfa.local

# 2. do the two pods agree — the one that matters
PODS=($(kubectl -n cfa get pods -l app.kubernetes.io/name=cfa-backend \
        -o jsonpath='{.items[*].metadata.name}'))
kubectl -n cfa port-forward pod/${PODS[0]} 18001:8000 &
kubectl -n cfa port-forward pod/${PODS[1]} 18002:8000 &
backend/Analyzer/.venv/bin/python deploy/checks/split.py \
    --a http://127.0.0.1:18001 --b http://127.0.0.1:18002

# 3. the machinery underneath
kubectl -n cfa port-forward svc/postgres 15432:5432 &
kubectl -n cfa port-forward svc/redis    16379:6379 &
backend/Analyzer/.venv/bin/python deploy/checks/coordination.py
```

Step 2 uses one port-forward per pod deliberately. Sent through the ingress,
the upload and the question land on the same pod most of the time, and the
check passes without having tested anything.

---

## 7. Case E — variations

**The model, in the cluster.** `deploy/minikube/base/ollama.yaml`, not applied
by default:

```bash
kubectl apply -n cfa -f deploy/minikube/base/ollama.yaml
kubectl -n cfa set env deploy/cfa-backend OLLAMA_BASE_URL=http://ollama:11434
kubectl -n cfa exec deploy/ollama -- ollama pull llama3.1:latest
kubectl -n cfa exec deploy/ollama -- ollama pull nomic-embed-text:latest
```

Read the sizing note at the top of that file first: an 8B model on CPU in a
minikube VM wants `--memory 12g` and still answers in minutes.

**CloudNativePG instead of the plain StatefulSet.** `deploy/cnpg-cluster.yaml`
is the same database as a real CNPG `Cluster` — three instances, failover,
a connection pooler if you want one:

```bash
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.27/releases/cnpg-1.27.0.yaml
kubectl apply -n cfa -f deploy/cnpg-cluster.yaml
```

The operator generates a `filing-analyzer-db-app` secret with a ready-made URI.
Point `database-url` at it and rewrite the scheme — CNPG emits `postgresql://`
and this app requires `postgresql+asyncpg://`, and refuses to start otherwise.
Then drop `postgres.yaml` from the kustomization.

**A real cluster.** Everything in `deploy/minikube/base/` transfers except
three things: `imagePullPolicy: Never` becomes a real registry reference, the
ingress annotations become your ingress controller's
([SCALING.md §10](SCALING.md#10-sticky-sessions-on-eks-concretely) has the ALB
set), and `pdb.yaml` should go into the kustomization — a PodDisruptionBudget
is right on a multi-node cluster and blocks drains on a single-node one.

**More than two replicas.** `replicas` is just a number once `CHROMA_HOST` is
set. Watch two things as it grows: `DB_POOL_SIZE + DB_MAX_OVERFLOW` multiplied
by the replica count against Postgres' `max_connections` (200 here), and the
fact that every replica queues against the same Ollama
([SCALING.md §14](SCALING.md#14-should-you-scale-out-at-all)).

---

## 8. Every setting that decides whether it scales

| Setting | Default | With N instances | What goes wrong if not |
| --- | --- | --- | --- |
| `JWT_SECRET_KEY` | *(a throwaway key)* | **the same value everywhere** | random 401s, refused handshakes, roughly `(N-1)/N` of the time |
| `CHROMA_HOST` | *(embedded)* | **a shared Chroma service** | "No filing is attached to this dossier yet" for a filing that uploaded fine |
| `CHROMA_PORT` | `8000` | matches the service | as above |
| `REDIS_URL` | *(off)* | shared Redis | nothing breaks — the cache and the summary leases just turn off |
| `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` | 5 + 10 | × N under `max_connections` | connections refused under load |
| `SHUTDOWN_DRAIN_SECONDS` | 120 | fits inside the grace period | answers cut off mid-write on every rollout |
| `STALE_RUN_MINUTES` | 30 | comfortably longer than a slow answer | a live run on another instance marked as interrupted |
| `SOCKETIO_MESSAGE_QUEUE_URL` | *(off)* | only once something broadcasts | nothing today; a silent delivery failure the day you add rooms |
| `CORS_ORIGINS` | `*` | the origins the browser actually uses | the page loads and the socket never connects |

The first two are the blockers. The rest are tuning, and the app's defaults are
reasonable for every case in this file.

---

## 9. The checks

Three scripts in [`deploy/checks/`](../deploy/checks/). Run them with the
backend's environment, which already has `aiohttp` and `python-socketio`:

```bash
backend/Analyzer/.venv/bin/python deploy/checks/<script>.py …
```

Each exits 0 or 1, so they drop into CI unchanged.

### `smoke.py` — does it work at all

```bash
… smoke.py --base http://cfa.local
… smoke.py --base "http://$(minikube ip)" --host cfa.local --origin http://cfa.local
```

Signs up, uploads a filing, asks about it over the socket, reads the ledger
back, deletes the dossier. Both protocols, the whole analyst path. If this
fails, nothing else is worth running.

`--origin` must be a value in `CORS_ORIGINS`, or the handshake is refused with a
400 before anything else is tested. `--host` sets the `Host` header for reaching
an ingress by IP without an `/etc/hosts` line.

### `split.py` — do two instances agree

```bash
… split.py --a http://127.0.0.1:18001 --b http://127.0.0.1:18002
```

Three breaks in one run, and it refuses to run if `--a` and `--b` are the same
URL, because then it proves nothing:

- **#1** — A mints a token, B accepts it
- **#2** — A ingests a filing, B answers from it
- **#5** — A and B write one dossier at once and nothing is lost

This is the check that matters. Everything `smoke.py` covers passes on a single
replica too.

### `coordination.py` — the machinery underneath

```bash
… coordination.py \
    --database-url postgresql+asyncpg://analyzer:analyzer@127.0.0.1:15432/filing_analyzer \
    --redis-url redis://127.0.0.1:16379/0
```

Goes below the API, straight to Postgres and Redis, importing the app's own
modules so it exercises the shipped code:

- twelve concurrent writers to one conversation, all landing, positions 1…12
- the interrupted-run sweep: an old stranded question closed out, a fresh one
  left alone, and running it twice changing nothing
- the Redis lease: taken, refused to a second holder, released
- the Postgres advisory lock: held, skipped by a second holder without blocking

It writes and deletes its own accounts. Point it at a development database.

---

## 10. What to test, per case

| | A | B | C | D |
| --- | :-: | :-: | :-: | :-: |
| `smoke.py` | ✅ | ✅ | ✅ | ✅ |
| `split.py` | — | — | ✅ | ✅ |
| `coordination.py` | ✅ | ✅ | ✅ | ✅ |
| `shared=True` in the logs | n/a | n/a | ✅ | ✅ |
| Sticky handshake | n/a | n/a | — | ✅ |
| Rollout survival | — | — | — | ✅ |
| Drain on shutdown | — | — | — | ✅ |

The three below the line need Kubernetes, and are worth doing by hand once.

**Sticky handshake.** Block WebSocket in the browser's devtools so the client
falls back to polling, then reload. Pass: the polling requests continue. Fail: a
connect/disconnect loop, which means the cookie is not reaching the backend —
check that `/socket.io` still routes straight to the `backend` Service in
`base/ingress.yaml` rather than through the frontend pod.

**Rollout survival.** Ask a long question, and while it is streaming:

```bash
kubectl -n cfa rollout restart deploy/cfa-backend
```

Pass: the answer is in the dossier when you reopen it. The terminating pod's log
should show the drain doing its job:

```
INFO  api.socket  Waiting up to 120s for 1 run(s) to finish
INFO  api.socket  All 1 in-flight run(s) finished
```

**Drain on shutdown.** Same thing with `kubectl -n cfa delete pod <one>`, and
watch the clock: the pod should exit *before* `terminationGracePeriodSeconds`
(180), not be killed at it. A pod that always takes exactly 180 seconds is being
SIGKILLed, and the answers it was holding are gone.

---

## 11. Breaking it on purpose

A check you have never seen fail is a check you do not know works. Each of these
is reversible in one command.

### Break #2 — take away the shared store

```bash
kubectl -n cfa set env deploy/cfa-backend CHROMA_HOST=""
kubectl -n cfa rollout status deploy/cfa-backend
```

Both pods come up `shared=False`, each with its own store. `split.py` then fails
on exactly one line and names the cause:

```
  Break #2 — one vector store
    PASS  A ingests the filing — 3 chunks
    FAIL  B answers from the filing A ingested — B has its own private store — set CHROMA_HOST
```

Breaks #1 and #5 keep passing, which is the point: the check isolates the fault
rather than going red all over. Undo:

```bash
kubectl -n cfa set env deploy/cfa-backend CHROMA_HOST-
```

### Break #1 — take away the shared secret

```bash
kubectl -n cfa set env deploy/cfa-backend JWT_SECRET_KEY=""
```

Each pod then invents its own key. `split.py` stops at the second check —
"B accepts A's token" — because there is no point testing anything else once
tokens do not travel. Undo with `JWT_SECRET_KEY-`.

### Redis — prove it fails soft

```bash
kubectl -n cfa scale deploy/redis --replicas=0
… smoke.py --base "http://$(minikube ip)" --host cfa.local --origin http://cfa.local
```

Pass: every check still passes. The cache is a cache, and the app is meant to be
correct without it. The pods say so:

```
WARNING  conversations.cache  Message cache disabled after a Redis error: Connection closed by server.
WARNING  core.leases          Leases disabled after a Redis error: Connection closed by server.
```

Note that both stay off until the pod restarts — reconnecting on every request
would mean paying a timeout per request for as long as Redis is down. So after
bringing Redis back:

```bash
kubectl -n cfa scale deploy/redis --replicas=1
kubectl -n cfa rollout restart deploy/cfa-backend      # to pick the cache back up
```

### Chroma — prove the API refuses to start without it

```bash
kubectl -n cfa scale deploy/chroma --replicas=0
kubectl -n cfa rollout restart deploy/cfa-backend
```

New pods sit in `Init:1/2`, the init container logging `waiting for chroma at
http://chroma:8000/…` every two seconds. That is deliberate: a store you cannot
reach is a broken deployment, and it should say so during the rollout rather
than serve requests that fail one at a time. `maxUnavailable: 0` means the old
pods keep serving throughout, so this is not an outage. Scale Chroma back and
the new pods start.

### Ollama — prove the failure is legible

Stop `ollama serve` on the host, then ask a question. `/api/health` still
answers 200 — it deliberately does not check the model host — and the question
comes back as an `error` event naming the connection failure. Uploads fail too,
since embedding is a model call.

---

## 12. Reading the logs

The lines worth grepping for after any deploy, and what each one means.

```bash
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 | grep -E "…"
```

| Line | Means |
| --- | --- |
| `VectorService ready (store=chroma:8000, shared=True…)` | the store is shared — replicas are legal |
| `VectorService ready (store=/app/data/…, shared=False…)` | embedded — **only correct at one instance** |
| `Tokens are signed with a key that dies with this process` | `JWT_SECRET_KEY` is unset; auth is already unreliable |
| `Cross-instance leases ready` | Redis is there; duplicate summaries are deduplicated |
| `Another instance is doing the startup housekeeping` | the advisory lock worked; this pod skipped, as designed |
| `Cleared N orphaned collection(s)` | pruning ran and found something |
| `Closed out N interrupted run(s) from a previous life` | the sweep healed questions a dead pod abandoned |
| `Waiting up to 120s for N run(s) to finish` | the drain is running; the pod is leaving politely |
| `N run(s) did not finish in time` | the drain expired — raise it, or the grace period |
| `Message cache disabled after a Redis error` | fail-soft worked; reads go to Postgres until restart |

The signing-key warning is emitted at *startup*, not at the first login. That is
deliberate: it used to appear whenever somebody first signed in, which on a
quiet pod could be hours after the rollout everybody had stopped watching.

---

## 13. Symptom → cause

| Symptom | Almost always |
| --- | --- |
| The page loads, the socket never connects | the browser's origin is not in `CORS_ORIGINS` — python-socketio answers the handshake 400 |
| Random 401s; signing in again fixes it for a while | `JWT_SECRET_KEY` differs between instances, or is unset |
| "No filing is attached to this dossier yet" for a filing that uploaded fine | `CHROMA_HOST` unset with more than one instance |
| An answer streamed to the browser is missing on reload | if instantly: `done` precedes the write, wait a moment. If permanently: you are on a build without the row lock |
| connect / disconnect / connect in the network tab | no sticky sessions, and the client fell back to polling |
| A question in the ledger with no answer | a pod died mid-run; the next start's sweep closes it out after `STALE_RUN_MINUTES` |
| Pod always takes exactly 180s to terminate | it is being SIGKILLed — the drain does not fit the grace period |
| `CrashLoopBackOff` with DuplicateTable | a build without the advisory lock around `create_all` |
| Chroma panics on `PORT` | a Service is injecting `CHROMA_PORT=tcp://…`; `enableServiceLinks: false` |
| Backend exits: `Cannot reach the Chroma server at …` | `CHROMA_HOST` names something that is not running |
| `ErrImageNeverPull` | the image is not inside minikube — `minikube image build`, not a host build |
| Health is fine, every question fails | Ollama unreachable; it needs `OLLAMA_HOST=0.0.0.0` |
| Connections refused under load | `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × replicas` exceeded `max_connections` |

---

## See also

- [SCALING.md](SCALING.md) — why each of these breaks, in detail
- [`deploy/minikube/README.md`](../deploy/minikube/README.md) — the manifests, one at a time
- [`deploy/checks/README.md`](../deploy/checks/README.md) — the scripts
- [DB-OPERATIONS.md](DB-OPERATIONS.md) — every read and write the app makes
- [SOCKETIO.md](SOCKETIO.md) — the real-time layer from first principles
