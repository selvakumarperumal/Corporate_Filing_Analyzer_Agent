# The stack on minikube

The same services `docker compose up` gives you, as Kubernetes objects, on a
one-node cluster on your machine — but run the way
[`docs/SCALING.md`](../../docs/SCALING.md) says they have to be run once there
is more than one of anything: **two API replicas**, one shared vector store, one
signing key from a Secret, a sticky ingress, and a shutdown long enough to
finish an answer.

The model is not in here. Ollama runs on the host, exactly as it does under
compose, and the pods reach it at `host.minikube.internal`.

---

## Contents

1. [Quick start](#quick-start)
2. [What gets created](#what-gets-created)
3. [How this answers docs/SCALING.md](#how-this-answers-docsscalingmd)
4. [Four decisions worth knowing](#four-decisions-worth-knowing)
5. [Everyday commands](#everyday-commands)
6. [Verifying it actually works](#verifying-it-actually-works)
7. [When it does not come up](#when-it-does-not-come-up)
8. [Teardown](#teardown)

---

## Quick start

**Prerequisites:** `minikube`, `kubectl`, a container runtime, and Ollama on
the host with the two models pulled and listening on more than loopback:

```bash
ollama pull llama3.1:latest
ollama pull nomic-embed-text:latest
OLLAMA_HOST=0.0.0.0 ollama serve
```

That last line matters. Ollama binds loopback by default, and a pod reaching
`host.minikube.internal` is not loopback.

**Then:**

```bash
./deploy/minikube/bootstrap.sh
```

It starts minikube if it is not running, enables the ingress addon, builds both
images *into the cluster's own runtime*, generates the JWT signing key, applies
everything, and waits for the rollout. It is safe to run again — the one thing
it will not redo is the signing key, because regenerating it would sign
everybody out.

Finish with the hosts entry it prints:

```bash
echo "$(minikube ip)  cfa.local" | sudo tee -a /etc/hosts
```

and open **http://cfa.local**.

### Or by hand

```bash
minikube start --cpus=4 --memory=6g
minikube addons enable ingress

# Both Deployments are imagePullPolicy: Never, so the images must exist inside
# the cluster. This builds them there — no registry, no push.
minikube image build -t cfa-backend:latest  backend
minikube image build -t cfa-frontend:latest frontend

kubectl create namespace cfa
kubectl -n cfa create secret generic cfa-secrets \
  --from-literal=jwt-secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \
  --from-literal=postgres-password='analyzer' \
  --from-literal=database-url='postgresql+asyncpg://analyzer:analyzer@postgres:5432/filing_analyzer'

kubectl apply -k deploy/minikube
kubectl -n cfa rollout status deploy/cfa-backend
```

Prefer a file to that one-liner?
`cp deploy/minikube/base/secret.example.yaml deploy/minikube/secret.yaml`, fill
in `jwt-secret`, then `kubectl apply -f deploy/minikube/secret.yaml`. That path
is gitignored; the example beside it stays placeholder-only.

### No sudo, no hosts entry

```bash
kubectl -n cfa port-forward svc/cfa-frontend 8080:8080
# → http://localhost:8080
```

This path goes through the frontend pod's own nginx proxy rather than the
ingress, which is fine at one replica and skips the sticky cookie. Both origins
are already in `CORS_ORIGINS`.

---

## What gets created

| Path | Objects | Notes |
| --- | --- | --- |
| `base/namespace.yaml` | Namespace `cfa` | the whole stack, one `delete` away |
| `base/config.yaml` | ConfigMaps `cfa-config`, `cfa-postgres` | every non-secret setting |
| `base/secret.example.yaml` | *(template)* | `jwt-secret`, `database-url`, `postgres-password` |
| `base/postgres.yaml` | StatefulSet + headless Service, 5Gi claim | accounts, dossiers, the ledger |
| `base/redis.yaml` | Deployment + Service | the message cache and the summary leases; no volume, by design |
| `base/chroma.yaml` | Deployment + Service + 10Gi claim | the filings, shared by every API pod |
| `base/backend.yaml` | Deployment + Service `backend` | the API — **two replicas**, no volumes |
| `base/frontend.yaml` | Deployment + Service | nginx and the static client |
| `base/ingress.yaml` | Ingress `cfa` | sticky cookie, long timeouts, 64m bodies |
| `base/ollama.yaml` | *(optional)* Deployment + Service + 20Gi claim | the model, in-cluster |
| `base/pdb.yaml` | *(optional)* PodDisruptionBudget | for a real cluster; it blocks drains on one node |
| `kustomization.yaml` | — | points at `base/` |
| `bootstrap.sh` | — | the quick start, as a script |

Only two things in the cluster hold state: the Postgres claim and the Chroma
claim. The API pods hold none, which is what makes `replicas` just a number.

The backend Service is named `backend`, not `cfa-backend`, and that is not a
naming slip: `frontend/nginx.conf` proxies to `http://backend:8000`, and the
frontend image is used here unmodified.

---

## How this answers `docs/SCALING.md`

The scaling document lists six breaks. Three were application bugs and are
fixed in the code; the rest are configuration, and this is that configuration.

| Break | Handled by | Where |
| --- | --- | --- |
| #1 JWT signing key | this deployment | `cfa-secrets/jwt-secret`, generated by `bootstrap.sh` |
| #2 Embedded vector store | code + this deployment | `CHROMA_HOST` in `base/config.yaml`, pointed at `base/chroma.yaml` |
| #3 Handshake routing | this deployment | cookie affinity in `base/ingress.yaml`, on the path that needs it |
| #4 In-flight runs on rollout | code + this deployment | 180s grace, 15s `preStop`, `SHUTDOWN_DRAIN_SECONDS=120` |
| #5 Message position race | code | a row lock in `record_message` — nothing to configure |
| #6 Broadcasting | code, off by default | `SOCKETIO_MESSAGE_QUEUE_URL`, blank until something broadcasts |
| §9 Postgres pool | this deployment | `DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=5`, server at `max_connections=200` |
| §9 Duplicate summaries | code | a Redis lease; degrades to per-process if Redis is gone |
| §9 Startup housekeeping | code | a Postgres advisory lock; one pod prunes and sweeps |

Break #4 is worth a second look, because it is the one the defaults get wrong in
a way you only notice later. Kubernetes' default grace period is 30 seconds; a
filing analysis on a local model regularly runs longer. Killed in between, the
ledger keeps the question and never gets the answer. Four numbers make one
budget here:

```
terminationGracePeriodSeconds  180   backend.yaml — the kubelet's patience
  preStop sleep                 15   backend.yaml — the ingress stops sending work
  SHUTDOWN_DRAIN_SECONDS       120   config.yaml — the app waits for live answers
  UVICORN_TIMEOUT_GRACEFUL…    150   config.yaml — uvicorn's own ceiling
```

15 + 120 has to fit inside 180. Raise the drain and you must raise the grace
period with it, or the kubelet kills the wait it was there to allow.

---

## Four decisions worth knowing

**The ingress sends `/api` and `/socket.io` straight to the backend.** Under
compose the browser only ever talks to nginx, which proxies both. Doing that
here would put the frontend pod between the client and the sticky cookie: each
polling request would arrive as a fresh connection through the frontend, and
kube-proxy would pick a backend endpoint per connection — a handshake split
across pods, which is Break #3 exactly. Routing those two prefixes at the
ingress puts the cookie on the hop that has to stay pinned. The page and the API
still share one origin, so nothing becomes cross-origin.

**`enableServiceLinks: false` on every pod.** Kubernetes injects a legacy
Docker-links variable per Service into every pod in the namespace, and the
`chroma` Service alone produces `CHROMA_PORT=tcp://10.x.x.x:8000`. Chroma's own
server reads `CHROMA_*` as configuration and panics on it — the first version of
these manifests crashlooped on exactly that. Nothing here uses those variables;
every setting arrives from `cfa-config`.

**`CORS_ORIGINS` is not only about CORS.** python-socketio checks the `Origin`
header on the handshake and answers 400 if it is not on the list — a same-origin
page included. Whatever host you type into the browser must appear in
`CORS_ORIGINS` in `base/config.yaml`, or the socket never connects while every
HTTP route keeps working. That failure looks like a broken app and reads like a
network problem.

**`host.minikube.internal` is how the pods reach the model.** minikube injects
it into CoreDNS; it is the `host.docker.internal` line from `docker-compose.yml`
by another name. To run the model in-cluster instead, apply `base/ollama.yaml`
and point `OLLAMA_BASE_URL` at `http://ollama:11434` — read the sizing warning
at the top of that file first.

---

## Everyday commands

```bash
# What is running
kubectl -n cfa get pods,svc,ingress,pvc

# Follow the API
kubectl -n cfa logs -f deploy/cfa-backend

# Pick up a code change: rebuild into the cluster, then restart
minikube image build -t cfa-backend:latest backend
kubectl -n cfa rollout restart deploy/cfa-backend

# Change a setting
kubectl -n cfa edit configmap cfa-config
kubectl -n cfa rollout restart deploy/cfa-backend   # ConfigMap env is read at start

# A psql prompt
kubectl -n cfa exec -it postgres-0 -- psql -U analyzer -d filing_analyzer
```

Note the second command in that list: `imagePullPolicy: Never` means the
kubelet uses whatever image with that tag is already inside minikube.
Rebuilding on your host's Docker daemon changes nothing the cluster can see —
it has to be `minikube image build`, or `eval $(minikube docker-env)` first.

---

## Verifying it actually works

The checks from [`docs/SCALING.md` §15](../../docs/SCALING.md#15-how-to-verify-it-actually-works),
as they apply here.

| Check | How | Pass |
| --- | --- | --- |
| Shared signing key | `kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend \| grep "signed with a key that dies"` | no output |
| Shared store | `kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend \| grep "VectorService ready"` | `shared=True` on both pods |
| Token survives a restart | sign in, `kubectl -n cfa rollout restart deploy/cfa-backend`, keep using the app | no re-login |
| The socket connects | browser devtools → Network → WS, one `/socket.io/` frame open | no connect/disconnect loop |
| Filings answer | upload `mock_10k_filing.txt`, then ask about it | never "No filing is attached to this dossier yet" |
| Grace period | `kubectl -n cfa delete pod -l app.kubernetes.io/name=cfa-backend` mid-answer | logs show `Waiting up to 120s…`; pod exits before 180s |
| Pool sizing | `SELECT count(*) FROM pg_stat_activity;` | well under 200 |
| Filings outlive the pods | delete both backend pods, reopen a dossier | the filing still answers |

These are the manual versions. [`deploy/checks/`](../checks/) has all of it as
three scripts, and [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md) covers
running them — plus how to break each thing on purpose and confirm the right
check goes red.

```bash
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py \
    --base "http://$(minikube ip)" --host cfa.local --origin http://cfa.local
```

### The one that actually proves it

Everything above passes on a single replica too. The test that proves the two
pods *share* a store rather than each having one is the upload/query split — and
it is worth forcing rather than waiting for, because the load balancer will
happily send both to the same pod all afternoon.

```bash
PODS=($(kubectl -n cfa get pods -l app.kubernetes.io/name=cfa-backend \
        -o jsonpath='{.items[*].metadata.name}'))
kubectl -n cfa port-forward pod/${PODS[0]} 18001:8000 &   # upload here
kubectl -n cfa port-forward pod/${PODS[1]} 18002:8000 &   # ask here
```

Sign up and upload a filing through `:18001`, then open a Socket.IO connection
to `:18002` and ask about it. Two things pass at once: pod B accepting a token
pod A minted (Break #1) and pod B answering from a filing pod A ingested
(Break #2). The logs should show the halves landing on different pods:

```
pod A   Ingested mock_10k_filing.txt -> 3 chunks (chat=…)
pod B   Retrieved 3 chunk(s) for 'What were total revenues…' (chat=…)
```

Same `chat=` id, different pods. That is the whole thing working.

---

## When it does not come up

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ErrImageNeverPull` | the image is not in minikube's runtime | `minikube image build -t cfa-backend:latest backend` |
| Backend stuck in `Init:0/2` | Chroma is not ready yet | `kubectl -n cfa logs deploy/chroma`; the init container waits 5 minutes then gives up |
| Chroma `CrashLoopBackOff`, panic on `PORT` | a Service is injecting `CHROMA_PORT=tcp://…` | `enableServiceLinks: false` — already set here; check it survived an edit |
| Backend exits with `Cannot reach the Chroma server` | `CHROMA_HOST` names something that is not there | `kubectl -n cfa get svc chroma`; unset `CHROMA_HOST` to fall back to embedded (one replica only) |
| `CreateContainerConfigError` | `cfa-secrets` does not exist yet | create it — see the quick start |
| Backend `CrashLoopBackOff`, logs show a connection refused | Postgres not up, or `database-url` disagrees with `cfa-postgres` | `kubectl -n cfa logs postgres-0`, then reconcile the two |
| Health is fine, every question fails | Ollama unreachable | `OLLAMA_HOST=0.0.0.0 ollama serve`; `kubectl -n cfa exec deploy/cfa-backend -- python -c "import urllib.request;print(urllib.request.urlopen('http://host.minikube.internal:11434/api/tags').status)"` |
| Page loads, socket never connects | the browser's origin is not in `CORS_ORIGINS` | add it to `cfa-config`, restart the backend |
| 404 from the ingress | no `/etc/hosts` entry, or the addon is off | `minikube addons enable ingress`; add the hosts line |
| 413 on upload | the ingress body limit | already 64m here; raise `proxy-body-size` for larger filings |
| Frontend `CrashLoopBackOff` at first apply | nginx resolves `backend` at startup and the Service did not exist yet | it self-heals on the next restart; `kubectl -n cfa rollout restart deploy/cfa-frontend` if impatient |
| Everything is `Pending` | minikube has no room | `minikube stop && minikube start --cpus=4 --memory=6g` |

---

## Teardown

```bash
# The stack, keeping the data
kubectl delete -k deploy/minikube

# The data too — accounts, the ledger, and every filing
kubectl delete namespace cfa

# The cluster
minikube delete
```

Deleting the namespace deletes both claims — `data-postgres-0` and
`chroma-data`. There is no copy of either anywhere else, so that is the command
that loses the accounts, the ledger and the filings together.

If you ran an earlier version of these manifests, a `cfa-filing-data` claim may
still be lying around from when the API kept filings on its own disk. Nothing
mounts it now: `kubectl -n cfa delete pvc cfa-filing-data`.
