# Deploying it, and proving it works

Two ways to run this app — one process on your machine, or two replicas on
Kubernetes — and how to test each, including how to break each one on purpose so
you know the test would have caught it.

There is no deploy script. Everything below is commands you run and can read,
because a deployment you cannot follow line by line is one you cannot debug.

The app is scalable, but only in the configuration that makes it so. Two
settings decide it, and everything here circles back to them:

```bash
JWT_SECRET_KEY=…      # the same value on every instance
CHROMA_HOST=chroma    # one vector store, not one per instance
```

Get either wrong with more than one instance and you get symptoms that look like
network problems and are not. [SCALING.md](SCALING.md) explains why; this file is
how you deploy it and how you check.

---

## Contents

1. [Which one do you want](#1-which-one-do-you-want)
2. [Before anything: the model](#2-before-anything-the-model)
3. [Case A — no containers](#3-case-a--no-containers)
4. [Case B — minikube](#4-case-b--minikube)
5. [Every setting that decides whether it scales](#5-every-setting-that-decides-whether-it-scales)
6. [The checks](#6-the-checks)
7. [What to test, per case](#7-what-to-test-per-case)
8. [Breaking it on purpose](#8-breaking-it-on-purpose)
9. [Reading the logs](#9-reading-the-logs)
10. [Symptom → cause](#10-symptom--cause)
11. [Teardown](#11-teardown)

---

## 1. Which one do you want

| | Case A | Case B |
| --- | --- | --- |
| | **no containers** | **minikube** |
| API instances | 1 | 2 |
| Vector store | embedded, on disk | shared service |
| Postgres | one you provide | StatefulSet + claim |
| Redis | optional | Deployment |
| Sticky sessions | n/a | yes, at the ingress |
| Graceful shutdown | no | yes |
| Good for | writing code | the real thing, locally |
| Ready in | seconds | ten minutes, mostly image builds |

**Case A is for changing the code.** Reload on save, a debugger that works,
nothing to rebuild. Nothing about scaling applies: one process is *allowed* to
keep filings on its own disk and sign with a throwaway key.

**Case B is the deployment.** Everything [SCALING.md](SCALING.md) argues for,
running: a shared signing key, a shared store, cookie affinity on the socket
path, and a shutdown long enough to finish an answer. If you are going to run
this anywhere for real, this is the shape it takes.

There is a `docker-compose.yml` too, and it still works — see the README's
*Running with Docker*. It is a convenience for using the app, not a deployment:
one API, no ingress, no stickiness, no termination budget. It is not covered
here because everything worth testing about a deployment is untestable on it.

---

## 2. Before anything: the model

Neither case runs Ollama. It lives on your machine.

```bash
ollama pull llama3.1:latest
ollama pull nomic-embed-text:latest
```

For Case A that is all. For Case B, Ollama also has to listen on more than
loopback, because a pod reaching `host.minikube.internal` is not loopback:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

Skipping that gives you a health check that passes and every question that
fails. Confirm before you go further:

```bash
curl -s http://localhost:11434/api/tags | head -c 200
```

---

## 3. Case A — no containers

One process, embedded vector store, and a Postgres you provide.

### 1. A database

Postgres is not optional — the schema uses `jsonb`, real foreign keys and a
pool, and the app refuses a non-async URL at startup rather than failing on the
first query. A container is the least trouble even here:

```bash
docker run -d --name cfa-pg -p 5432:5432 \
  -e POSTGRES_USER=analyzer \
  -e POSTGRES_PASSWORD=analyzer \
  -e POSTGRES_DB=filing_analyzer \
  postgres:17-bookworm
```

The app's default `DATABASE_URL` already points at exactly that.

### 2. The API

```bash
cd backend/Analyzer
uv sync
uv run uvicorn main:asgi_app --reload --port 8000
```

Optional, in a `.env` beside it — worth setting even at one instance, because
without it every restart signs you out:

```bash
JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
REDIS_URL=redis://localhost:6379/0     # only if you have one; the cache is optional
```

### 3. The workbench

```bash
cd frontend
python3 -m http.server 5500
```

→ **http://localhost:5500**

`frontend/config.js` leaves `__BACKEND_URL__` unset, so the page falls back to
`http://localhost:8000`, and `CORS_ORIGINS` defaults to `*`, so the socket
connects from wherever you serve the page.

### 4. Check it

```bash
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py --base http://localhost:8000
```

The startup log tells you what you are running, and both lines are correct here:

```
WARNING  main  Tokens are signed with a key that dies with this process…
INFO     …vector_store  VectorService ready (store=…/backend/data/chroma_db, shared=False, …)
```

`shared=False` is right for one process and a bug for two. That is the whole
difference between this case and the next one.

---

## 4. Case B — minikube

Two API replicas, one shared store, a signing key from a Secret, cookie affinity
on the socket path, and a 180-second termination budget.

Seven steps. Read them once — the last three are the ones you will repeat.

### 1. A cluster

```bash
minikube start --cpus=4 --memory=6g
```

6 GB is a sensible floor with Ollama on the host: two API replicas, Postgres,
Redis, Chroma and the ingress controller all have to fit. If the cluster already
exists, minikube will not resize it — `minikube delete` first if you need to.

### 2. The ingress controller

```bash
minikube addons enable ingress
```

Without it the `Ingress` object is inert and `http://cfa.local` goes nowhere.
This is also what terminates the sticky cookie, so it is not optional decoration.

### 3. Both images, inside the cluster

```bash
minikube image build -t cfa-backend:latest  backend
minikube image build -t cfa-frontend:latest frontend
```

**`minikube image build`, not `docker build`.** The manifests use
`imagePullPolicy: Never`, so the kubelet uses whatever image with that tag is
already inside minikube's own container runtime. Building on your host daemon
changes nothing the cluster can see, and you get `ErrImageNeverPull`.

The backend build resolves the whole dependency tree and takes a few minutes the
first time.

### 4. The namespace and the Secret

```bash
kubectl create namespace cfa

kubectl -n cfa create secret generic cfa-secrets \
  --from-literal=jwt-secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \
  --from-literal=postgres-password='analyzer' \
  --from-literal=database-url='postgresql+asyncpg://analyzer:analyzer@postgres:5432/filing_analyzer'
```

The signing key is generated here rather than committed, which is why the Secret
is not in the kustomization: a key in git is a key everyone has, and an unset key
is a key that changes on every restart. `database-url` must agree with
`POSTGRES_USER` and `POSTGRES_DB` in `base/config.yaml`, and its `+asyncpg`
scheme is not optional — the app refuses a plain `postgresql://` at startup.

Prefer a file? `deploy/minikube/base/secret.example.yaml` is the same thing as
YAML; copy it to `deploy/minikube/secret.yaml` (gitignored), fill in
`jwt-secret`, and `kubectl apply -f` it.

**Re-running this later:** `kubectl create secret` fails if it already exists,
which is the safe behaviour — regenerating the key signs everybody out. To
rotate it deliberately, delete it first.

### 5. Apply

```bash
kubectl apply -k deploy/minikube
```

Order does not matter: kubectl sorts by kind, so the Namespace, ConfigMaps and
Services exist before anything that needs them.

### 6. Wait, in this order

```bash
kubectl -n cfa rollout status deploy/chroma       --timeout=5m
kubectl -n cfa rollout status deploy/cfa-backend  --timeout=10m
kubectl -n cfa rollout status deploy/cfa-frontend --timeout=2m
```

Chroma first, because the API heartbeats the store during startup and will not
come up without it — which is why `backend.yaml` has an init container that
waits. The backend's first start also imports the graph and the model clients,
so give it room.

### 7. Reach it

```bash
echo "$(minikube ip)  cfa.local" | sudo tee -a /etc/hosts
```

→ **http://cfa.local**

No sudo, no hosts entry? Port-forward the workbench instead:

```bash
kubectl -n cfa port-forward svc/cfa-frontend 8080:8080
# → http://localhost:8080
```

Both origins are already in `CORS_ORIGINS`. Anything else — a raw `minikube ip`,
a different port — has to be added there, or the page will load and the socket
will never connect.

### The one line to check before trusting it

```bash
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 \
  | grep "VectorService ready"
```

Two lines, both `shared=True`. One `shared=False` among them is a deployment
that will lose filings, and every other check can still pass.

### Picking up a code change

```bash
minikube image build -t cfa-backend:latest backend
kubectl -n cfa rollout restart deploy/cfa-backend
```

A ConfigMap change needs the same restart — environment is read at start:

```bash
kubectl -n cfa edit configmap cfa-config
kubectl -n cfa rollout restart deploy/cfa-backend
```

What each manifest does and why is in
[`deploy/minikube/README.md`](../deploy/minikube/README.md).

---

## 5. Every setting that decides whether it scales

| Setting | Default | With N instances | What goes wrong if not |
| --- | --- | --- | --- |
| `JWT_SECRET_KEY` | *(a throwaway key)* | **the same value everywhere** | random 401s and refused handshakes, roughly `(N-1)/N` of the time |
| `CHROMA_HOST` | *(embedded)* | **a shared Chroma service** | "No filing is attached to this dossier yet" for a filing that uploaded fine |
| `CHROMA_PORT` | `8000` | matches the service | as above |
| `REDIS_URL` | *(off)* | shared Redis | nothing breaks — the cache and the summary leases just turn off |
| `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` | 5 + 10 | × N under `max_connections` | connections refused under load |
| `SHUTDOWN_DRAIN_SECONDS` | 120 | fits inside the grace period | answers cut off mid-write on every rollout |
| `STALE_RUN_MINUTES` | 30 | longer than a slow answer | a live run on another instance marked interrupted |
| `SOCKETIO_MESSAGE_QUEUE_URL` | *(off)* | only once something broadcasts | nothing today; a silent delivery failure the day you add rooms |
| `CORS_ORIGINS` | `*` | the origins the browser actually uses | the page loads and the socket never connects |

The first two are the blockers. The rest is tuning, and the defaults are
reasonable for both cases here.

---

## 6. The checks

Three scripts in [`deploy/checks/`](../deploy/checks/). Run them with the
backend's own environment, which already has `aiohttp` and `python-socketio`:

```bash
backend/Analyzer/.venv/bin/python deploy/checks/<script>.py …
```

Each exits 0 or 1, so they drop into CI unchanged.

### `smoke.py` — does it work at all

```bash
# Case A
… smoke.py --base http://localhost:8000

# Case B
… smoke.py --base "http://$(minikube ip)" --host cfa.local --origin http://cfa.local
```

Signs up, uploads a filing, asks about it over the socket, reads the ledger
back, deletes the dossier. Both protocols, the whole analyst path. If this
fails, nothing else is worth running.

`--origin` must be a value in `CORS_ORIGINS`, or the handshake is refused with a
400 before anything else gets tested. `--host` sets the `Host` header, for
reaching an ingress by IP without an `/etc/hosts` line.

### `split.py` — do two instances agree

Case B only, and it needs the two pods addressed separately:

```bash
PODS=($(kubectl -n cfa get pods -l app.kubernetes.io/name=cfa-backend \
        -o jsonpath='{.items[*].metadata.name}'))
kubectl -n cfa port-forward pod/${PODS[0]} 18001:8000 &
kubectl -n cfa port-forward pod/${PODS[1]} 18002:8000 &

backend/Analyzer/.venv/bin/python deploy/checks/split.py \
    --a http://127.0.0.1:18001 --b http://127.0.0.1:18002
```

One port-forward per pod, deliberately. Sent through the ingress, the upload and
the question land on the same pod most of the time and the check passes without
having tested anything. It refuses to run if `--a` and `--b` are the same URL,
for the same reason.

Three breaks in one run:

- **#1** — A mints a token, B accepts it
- **#2** — A ingests a filing, B answers from it
- **#5** — A and B write one dossier at once and nothing is lost

This is the check that matters. Everything `smoke.py` covers passes on a single
replica too.

### `coordination.py` — the machinery underneath

```bash
# Case B: port-forward the two stores first
kubectl -n cfa port-forward svc/postgres 15432:5432 &
kubectl -n cfa port-forward svc/redis    16379:6379 &

backend/Analyzer/.venv/bin/python deploy/checks/coordination.py \
    --database-url postgresql+asyncpg://analyzer:analyzer@127.0.0.1:15432/filing_analyzer \
    --redis-url redis://127.0.0.1:16379/0

# Case A: your local Postgres, and no Redis
backend/Analyzer/.venv/bin/python deploy/checks/coordination.py \
    --database-url postgresql+asyncpg://analyzer:analyzer@127.0.0.1:5432/filing_analyzer \
    --redis-url ""
```

Goes below the API, straight to Postgres and Redis, importing the app's own
modules so it exercises the shipped code:

- twelve concurrent writers to one conversation, all landing, positions 1…12
- the interrupted-run sweep: an old stranded question closed out, a fresh one
  left alone, and running it twice changing nothing
- the Redis lease: taken, refused to a second holder, released
- the Postgres advisory lock: held, skipped by a second holder without blocking

An empty `--redis-url` skips the lease section rather than failing it — leases
are best effort by design. A *configured* Redis that does not answer is a real
failure.

It writes and deletes its own accounts. Point it at a development database.

---

## 7. What to test, per case

| | Case A | Case B |
| --- | :-: | :-: |
| `smoke.py` | ✅ | ✅ |
| `coordination.py` | ✅ | ✅ |
| `split.py` | — | ✅ |
| `shared=True` in the logs | n/a | ✅ |
| Sticky handshake | n/a | ✅ |
| Rollout survival | — | ✅ |
| Drain on shutdown | — | ✅ |

The last three need Kubernetes and are worth doing by hand once, and
[TESTING-SCALING.md](TESTING-SCALING.md) takes each of them — and everything
else in [SCALING.md](SCALING.md) — through its negative as well.

**Sticky handshake.** Block WebSocket in the browser's devtools so the client
falls back to polling, then reload. Pass: the polling requests continue. Fail: a
connect/disconnect loop, which means the cookie is not reaching the backend —
check that `/socket.io` still routes straight to the `backend` Service in
`base/ingress.yaml` rather than through the frontend pod.

**Rollout survival.** Ask a long question, and while it is streaming:

```bash
kubectl -n cfa rollout restart deploy/cfa-backend
```

Pass: the answer is in the dossier when you reopen it. The terminating pod
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

## 8. Breaking it on purpose

A check you have never seen fail is a check you do not know works. Each of these
is Case B, and each is reversible in one command.

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

Each pod then invents its own key. `split.py` stops at the second check — "B
accepts A's token" — because there is no point testing anything else once tokens
do not travel between instances. Undo with `JWT_SECRET_KEY-`.

### Redis — prove it fails soft

```bash
kubectl -n cfa scale deploy/redis --replicas=0
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py \
    --base "http://$(minikube ip)" --host cfa.local --origin http://cfa.local
```

Pass: every check still passes. The cache is a cache, and the app is meant to be
correct without it. The pods say so:

```
WARNING  conversations.cache  Message cache disabled after a Redis error: Connection closed by server.
WARNING  core.leases          Leases disabled after a Redis error: Connection closed by server.
```

Both stay off until the pod restarts — reconnecting on every request would mean
paying a timeout per request for as long as Redis is down. So bring it back with
a restart:

```bash
kubectl -n cfa scale deploy/redis --replicas=1
kubectl -n cfa rollout restart deploy/cfa-backend
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

## 9. Reading the logs

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
deliberate: it used to appear whenever somebody first signed in, which on a quiet
pod could be hours after the rollout everybody had stopped watching.

---

## 10. Symptom → cause

| Symptom | Almost always |
| --- | --- |
| The page loads, the socket never connects | the browser's origin is not in `CORS_ORIGINS` — python-socketio answers the handshake 400 |
| Random 401s; signing in again fixes it for a while | `JWT_SECRET_KEY` differs between instances, or is unset |
| "No filing is attached to this dossier yet" for a filing that uploaded fine | `CHROMA_HOST` unset with more than one instance |
| An answer streamed to the browser is missing on reload | if instantly: `done` precedes the write, wait a moment. If permanently: a build without the row lock |
| connect / disconnect / connect in the network tab | no sticky sessions, and the client fell back to polling |
| A question in the ledger with no answer | a pod died mid-run; the next start's sweep closes it out after `STALE_RUN_MINUTES` |
| Pod always takes exactly 180s to terminate | it is being SIGKILLed — the drain does not fit the grace period |
| `CrashLoopBackOff` with DuplicateTable | a build without the advisory lock around `create_all` |
| Chroma panics on `PORT` | a Service is injecting `CHROMA_PORT=tcp://…`; `enableServiceLinks: false` |
| Backend exits: `Cannot reach the Chroma server at …` | `CHROMA_HOST` names something that is not running |
| Backend stuck in `Init:0/2` or `Init:1/2` | it is waiting for Postgres or Chroma; read that init container's log |
| `ErrImageNeverPull` | the image is not inside minikube — `minikube image build`, not a host build |
| `CreateContainerConfigError` | `cfa-secrets` does not exist yet — step 4 |
| 404 from the ingress | no `/etc/hosts` entry, or the addon is off |
| Health is fine, every question fails | Ollama unreachable; it needs `OLLAMA_HOST=0.0.0.0` |
| Connections refused under load | `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × replicas` exceeded `max_connections` |

---

## 11. Teardown

**Case A.** Stop the two processes. The database keeps running:

```bash
docker rm -f cfa-pg          # and the accounts, ledger and all
rm -rf backend/data          # the filings
```

**Case B.**

```bash
kubectl delete -k deploy/minikube    # the stack, keeping the data
kubectl delete namespace cfa         # the data too — accounts, ledger, filings
minikube delete                      # the cluster
```

Deleting the namespace deletes both claims, `data-postgres-0` and `chroma-data`.
There is no copy of either anywhere else, so that is the command that loses the
accounts, the ledger and the filings together.

---

## See also

- [SCALING.md](SCALING.md) — why each of these breaks, in detail
- [TESTING-SCALING.md](TESTING-SCALING.md) — provoking each break by hand, including the ones no script covers
- [`deploy/minikube/README.md`](../deploy/minikube/README.md) — what each manifest does, and why
- [`deploy/checks/README.md`](../deploy/checks/README.md) — the scripts
- [DB-OPERATIONS.md](DB-OPERATIONS.md) — every read and write the app makes
- [SOCKETIO.md](SOCKETIO.md) — the real-time layer from first principles
