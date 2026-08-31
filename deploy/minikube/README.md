# The stack on minikube

The same services `docker compose up` gives you, as Kubernetes objects, on a
one-node cluster on your machine — but run the way
[`docs/SCALING.md`](../../docs/SCALING.md) says they have to be run once there
is more than one of anything: **two API replicas**, one shared vector store, one
signing key from a Secret, a sticky gateway, and a shutdown long enough to
finish an answer.

The model is not in here. Ollama runs on the host, exactly as it does under
compose, and the pods reach it at `host.minikube.internal`.

---

## Contents

1. [Deploying this](#deploying-this)
2. [What gets created](#what-gets-created)
3. [How this answers docs/SCALING.md](#how-this-answers-docsscalingmd)
4. [Four decisions worth knowing](#four-decisions-worth-knowing)
5. [Everyday commands](#everyday-commands)
6. [Verifying it actually works](#verifying-it-actually-works)
7. [When it does not come up](#when-it-does-not-come-up)
8. [Teardown](#teardown)

---

## Deploying this

The runbook lives in [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md) — seven
steps, every command written out, plus what to test and how to break each part
on purpose. The short version, once the cluster and images exist:

```bash
# once per cluster: the Gateway API CRDs, and Istio to serve them
kubectl apply -k "github.com/kubernetes-sigs/gateway-api/config/crd?ref=v1.2.1"
istioctl install --set profile=minimal -y

kubectl apply -k deploy/minikube
kubectl -n cfa rollout status deploy/chroma      --timeout=5m
kubectl -n cfa rollout status deploy/cfa-backend --timeout=10m

# Istio provisions the data plane from the Gateway object
kubectl -n cfa rollout status deploy/cfa-istio   --timeout=5m
minikube tunnel                                  # leave running, needs sudo
```

This file is the other half: what each object is, and why it is shaped the way
it is.

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
| `base/gateway.yaml` | Gateway + HTTPRoute `cfa`, DestinationRule `backend-sticky` | the way in: path routing, no request timeout, sticky cookie |
| `base/ollama.yaml` | *(optional)* Deployment + Service + 20Gi claim | the model, in-cluster |
| `base/pdb.yaml` | *(optional)* PodDisruptionBudget | for a real cluster; it blocks drains on one node |
| `kustomization.yaml` | — | points at `base/` |

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
| #1 JWT signing key | this deployment | `cfa-secrets/jwt-secret`, generated when you create the Secret |
| #2 Embedded vector store | code + this deployment | `CHROMA_HOST` in `base/config.yaml`, pointed at `base/chroma.yaml` |
| #3 Handshake routing | this deployment | `consistentHash` cookie in `base/gateway.yaml`, on the path that needs it |
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
  preStop sleep                 15   backend.yaml — the gateway stops sending work
  SHUTDOWN_DRAIN_SECONDS       120   config.yaml — the app waits for live answers
  UVICORN_TIMEOUT_GRACEFUL…    150   config.yaml — uvicorn's own ceiling
```

15 + 120 has to fit inside 180. Raise the drain and you must raise the grace
period with it, or the kubelet kills the wait it was there to allow.

---

## Four decisions worth knowing

**The HTTPRoute sends `/api` and `/socket.io` straight to the backend.** Under
compose the browser only ever talks to nginx, which proxies both. Doing that
here would put the frontend pod between the client and the sticky cookie: each
polling request would arrive as a fresh connection through the frontend, and
kube-proxy would pick a backend endpoint per connection — a handshake split
across pods, which is Break #3 exactly. Routing those two prefixes at the
gateway puts the cookie on the hop that has to stay pinned. The page and the API
still share one origin, so nothing becomes cross-origin.

**Stickiness is a DestinationRule, not a field on the route.** Gateway API does
have a portable way to say it — `sessionPersistence` on an HTTPRoute rule — but
it lives in the experimental channel, which is a different set of CRDs from the
ones installed above. Istio's `DestinationRule` is in the standard install and
has done consistent hashing on a cookie for years, so that is what
`base/gateway.yaml` uses. On a different controller, `sessionPersistence` is the
field to port it to.

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
kubectl -n cfa get pods,svc,gateway,httproute,pvc

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

Three scripts in [`deploy/checks/`](../checks/), and
[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#6-the-checks) covers running
them — including how to break each thing on purpose and watch the right check go
red.

The one line to read before trusting a two-replica deployment:

```bash
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 \
  | grep "VectorService ready"
```

Two lines, both `shared=True`. One `shared=False` among them is a deployment
that will lose filings, and every other check can still pass — which is why
this is the first thing to look at and not the last.

---

## When it does not come up

The symptoms specific to these manifests. The wider catalogue — including the
application-level ones — is in
[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#10-symptom--cause).

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ErrImageNeverPull` | the image is not in minikube's runtime | `minikube image build -t cfa-backend:latest backend` — a host build changes nothing the cluster can see |
| `CreateContainerConfigError` | `cfa-secrets` does not exist yet | create it, step 4 of the runbook |
| Backend stuck in `Init:0/2` | waiting for Postgres | `kubectl -n cfa logs postgres-0` |
| Backend stuck in `Init:1/2` | waiting for Chroma | `kubectl -n cfa logs deploy/chroma`; the init container gives up after 5 minutes |
| Chroma `CrashLoopBackOff`, panic on `PORT` | a Service is injecting `CHROMA_PORT=tcp://…` | `enableServiceLinks: false` — already set here; check it survived an edit |
| Backend `CrashLoopBackOff`, connection refused | `database-url` disagrees with `cfa-postgres` | reconcile the Secret with the ConfigMap |
| Frontend `CrashLoopBackOff` at first apply | nginx resolves `backend` at startup and the Service did not exist yet | it self-heals; `kubectl -n cfa rollout restart deploy/cfa-frontend` if impatient |
| 404 from the gateway | no `/etc/hosts` entry, or no tunnel | `minikube tunnel`, then add the `/etc/hosts` line |
| `cfa.local` refuses the connection | the Gateway Service has no address | `kubectl -n cfa get gateway cfa` — `PROGRAMMED` must be True, and `minikube tunnel` must be running |
| Gateway stuck without an address | no controller for the `istio` GatewayClass | `kubectl get gatewayclass istio`; `istioctl install --set profile=minimal -y` |
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
