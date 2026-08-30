#!/usr/bin/env bash
# One command from a cold machine to a running workbench.
#
#     ./deploy/minikube/bootstrap.sh
#
# Idempotent: run it again after changing a manifest and it re-applies. The
# only thing it will not do twice is overwrite the JWT signing key, because
# doing so would invalidate every token already issued.
set -euo pipefail

NAMESPACE="${NAMESPACE:-cfa}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"

say() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

# ── 1. A cluster ─────────────────────────────────────────────────────────
# 6 GB is a sensible floor with Ollama on the host: two API replicas, postgres,
# redis, chroma and the ingress controller all have to fit. Running Ollama
# in-cluster needs far more — see base/ollama.yaml.
if ! minikube status >/dev/null 2>&1; then
  say "Starting minikube"
  minikube start --cpus=4 --memory=6g
fi

say "Enabling the ingress addon"
minikube addons enable ingress

# ── 2. The images, built into minikube's own daemon ──────────────────────
# Both Deployments use imagePullPolicy: Never, so the images have to exist
# inside the cluster's container runtime. `minikube image build` puts them
# there directly — no registry, no push.
say "Building cfa-backend:latest"
minikube image build -t cfa-backend:latest "${REPO_ROOT}/backend"

say "Building cfa-frontend:latest"
minikube image build -t cfa-frontend:latest "${REPO_ROOT}/frontend"

# ── 3. The namespace and the Secret ──────────────────────────────────────
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 \
  || kubectl create namespace "${NAMESPACE}"

if kubectl -n "${NAMESPACE}" get secret cfa-secrets >/dev/null 2>&1; then
  say "Secret cfa-secrets already exists — leaving it alone"
else
  say "Creating cfa-secrets with a freshly generated signing key"
  # docs/SCALING.md, Break #1. Generated here rather than committed: a key in
  # git is a key everyone has, and an unset key is a key that changes on every
  # restart and signs everybody out.
  kubectl -n "${NAMESPACE}" create secret generic cfa-secrets \
    --from-literal=jwt-secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    --from-literal=postgres-password='analyzer' \
    --from-literal=database-url='postgresql+asyncpg://analyzer:analyzer@postgres:5432/filing_analyzer'
fi

# ── 4. The stack ─────────────────────────────────────────────────────────
say "Applying manifests"
kubectl apply -k "${HERE}"

# Chroma first: the API heartbeats it during startup and will not come up
# without it, which is why backend.yaml has an init container that waits.
say "Waiting for the vector store"
kubectl -n "${NAMESPACE}" rollout status deploy/chroma --timeout=5m

say "Waiting for the API (first start pulls in the graph and the model clients)"
kubectl -n "${NAMESPACE}" rollout status deploy/cfa-backend --timeout=10m
kubectl -n "${NAMESPACE}" rollout status deploy/cfa-frontend --timeout=2m

# The one line that says whether this is a deployment that can be replicated.
say "Vector store mode (want shared=True on every pod)"
kubectl -n "${NAMESPACE}" logs -l app.kubernetes.io/name=cfa-backend --tail=-1 \
  | grep "VectorService ready" || true

# ── 5. How to reach it ───────────────────────────────────────────────────
IP="$(minikube ip)"
if grep -q '[[:space:]]cfa\.local\b' /etc/hosts 2>/dev/null; then
  say "Ready — http://cfa.local"
else
  say "Ready. One step left, which needs sudo:"
  printf '\n    echo "%s  cfa.local" | sudo tee -a /etc/hosts\n\n' "${IP}"
  printf 'Then open http://cfa.local\n'
  printf 'Or skip the hosts entry entirely:\n'
  printf '    kubectl -n %s port-forward svc/cfa-frontend 8080:8080\n' "${NAMESPACE}"
  printf '    # …and open http://localhost:8080\n'
fi
