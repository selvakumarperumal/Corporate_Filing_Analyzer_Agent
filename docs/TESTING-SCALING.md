# Testing what breaks at N instances

[SCALING.md](SCALING.md) says what breaks and why. [DEPLOYMENT.md](DEPLOYMENT.md)
runs three scripts that say whether a deployment is one of the good ones. This
file is the part in between: how to **provoke** each break yourself, watch it
happen, and prove the fix — by hand, one problem at a time.

> A check you have never seen fail is a check you do not know works. Most of
> what SCALING.md describes is silent when it goes wrong: no 500, no error
> toast, no red pod — a filing that answers nothing, an answer that was never
> stored, a login that fails one request in two. The only way to trust the
> handling is to have watched the same test go red with the handling removed.

There are no test scripts in this file, deliberately. Everything below is a rig
you can stand up in a few minutes, one thing to do to it, and the single line of
output that tells you which way it went. The scripts in
[`deploy/checks/`](../deploy/checks/) already cover what automates cleanly; this
file covers what they cannot reach — stickiness, drain timing, the schema race,
pool exhaustion, the summary lease — and the negatives for the parts they do.

| Where to look | For |
| --- | --- |
| **this file** | how to provoke each break and read the result |
| [SCALING.md](SCALING.md) | what each break *is*, and where it is handled |
| [DEPLOYMENT.md](DEPLOYMENT.md) | how to stand the deployment up in the first place |
| [`deploy/checks/`](../deploy/checks/) | the three scripts, for the paths that automate |
| [DB-OPERATIONS.md](DB-OPERATIONS.md) | every read and write, when a query below surprises you |

---

## Contents

1. [Three rigs, and what each proves](#1-three-rigs-and-what-each-proves)
2. [The coverage matrix](#2-the-coverage-matrix)
3. [Ground rules](#3-ground-rules)
4. [Break #1 — the shared signing key](#4-break-1--the-shared-signing-key)
5. [Break #2 — the shared vector store](#5-break-2--the-shared-vector-store)
6. [Break #3 — the sticky handshake](#6-break-3--the-sticky-handshake)
7. [Break #4 — the drain and the sweep](#7-break-4--the-drain-and-the-sweep)
8. [Break #5 — two writers, one position](#8-break-5--two-writers-one-position)
9. [Break #6 — broadcasting, before the feature exists](#9-break-6--broadcasting-before-the-feature-exists)
10. [Postgres connections](#10-postgres-connections)
11. [Rolling summaries and the lease](#11-rolling-summaries-and-the-lease)
12. [Startup housekeeping, and the schema race](#12-startup-housekeeping-and-the-schema-race)
13. [Redis — proving it fails soft](#13-redis--proving-it-fails-soft)
14. [Ordering, and the week-old pod](#14-ordering-and-the-week-old-pod)
15. [SQL to keep beside you](#15-sql-to-keep-beside-you)
16. [Load, and what it will not tell you](#16-load-and-what-it-will-not-tell-you)
17. [False passes](#17-false-passes)
18. [The evidence table](#18-the-evidence-table)
19. [A session, in order](#19-a-session-in-order)

---

## 1. Three rigs, and what each proves

Almost every test below runs on one of three shapes. Build the cheapest one that
can actually show the fault — a break tested on a rig that cannot express it is
worse than not testing it, because it leaves you believing something.

| | **Rig 1** | **Rig 2** | **Rig 3** |
| --- | --- | --- | --- |
| | one process | two processes, one machine | minikube, two replicas |
| Set up in | seconds | five minutes | see [DEPLOYMENT.md §4](DEPLOYMENT.md#4-case-b--minikube) |
| Instances | 1 | 2 | 2 |
| You choose which instance gets a request | n/a | **yes — by port** | via `port-forward` |
| Shared Postgres / Redis | yes | yes | yes |
| Shared vector store | n/a | yes, if you run one | yes |
| Load balancer, stickiness | no | no | yes |
| Rollouts, drains, grace periods | SIGTERM only | SIGTERM only | the real thing |
| Good for | Breaks #5, the drain, Redis fail-soft | Breaks #1, #2, #5, leases, pools | everything, and the only rig for #3 and #4 |

### Rig 1 — one process

[Case A of DEPLOYMENT.md](DEPLOYMENT.md#3-case-a--no-containers), unchanged. It
tests less than you would think and more than you would expect: Break #5 needs
no second instance, and neither does the drain.

### Rig 2 — two processes on your machine

The workhorse. Shared stores once:

```bash
docker run -d --name cfa-pg -p 5432:5432 \
  -e POSTGRES_USER=analyzer -e POSTGRES_PASSWORD=analyzer \
  -e POSTGRES_DB=filing_analyzer postgres:17-bookworm
docker run -d --name cfa-redis  -p 6379:6379 redis:7-alpine
docker run -d --name cfa-chroma -p 8000:8000 chromadb/chroma:1.5.9
```

Then two API processes, sharing everything a deployment would share:

```bash
export KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')
export DATABASE_URL=postgresql+asyncpg://analyzer:analyzer@localhost:5432/filing_analyzer
export REDIS_URL=redis://localhost:6379/0
export CHROMA_HOST=localhost CHROMA_PORT=8000

# terminal 1 — instance A
cd backend/Analyzer && JWT_SECRET_KEY=$KEY uv run uvicorn main:asgi_app --port 8001

# terminal 2 — instance B, same variables
cd backend/Analyzer && JWT_SECRET_KEY=$KEY uv run uvicorn main:asgi_app --port 8002
```

Four things about this rig are load-bearing:

- **No `--reload`.** A reload is a restart, and half of these tests are about
  what a restart does.
- **Environment beats the `.env`** beside `main.py` (`model_config =
  {"env_file": ".env"}`), so exporting a variable is enough — no file to edit
  and forget.
- **Nothing balances between 8001 and 8002**, which is the point. You decide
  which instance sees each request, so no stickiness can quietly turn a
  two-instance test into a one-instance test.
- **Both processes resolve the *embedded* store to the same directory.**
  `core/paths.py` fixes it at `backend/data/chroma_db`, so unsetting
  `CHROMA_HOST` here does not reproduce two pods with two disks — it reproduces
  two writers on one SQLite file, which is the *other* reason embedded mode does
  not scale. For the "filing invisible to the other instance" symptom, give the
  second process its own tree (`git worktree add ../cfa-b`, then run instance B
  from there — `backend/data` is gitignored, so it starts empty) or use Rig 3.

### Rig 3 — minikube, two replicas

The only rig with an ingress, a kubelet and a termination budget, so the only
one that can test Break #3 and the whole of Break #4. Two commands are worth
keeping in a scratch file, because nearly every test needs them:

```bash
# address each pod separately — never test a split through the ingress
PODS=($(kubectl -n cfa get pods -l app.kubernetes.io/name=cfa-backend \
        -o jsonpath='{.items[*].metadata.name}'))
kubectl -n cfa port-forward pod/${PODS[0]} 18001:8000 &
kubectl -n cfa port-forward pod/${PODS[1]} 18002:8000 &

# read every pod's log, from the first line, with the pod name in front
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 --prefix
```

---

## 2. The coverage matrix

| # | What breaks | Rig 1 | Rig 2 | Rig 3 | Section |
| --- | --- | :-: | :-: | :-: | --- |
| 1 | JWT signing key | restart only | ✅ | ✅ | [§4](#4-break-1--the-shared-signing-key) |
| 2 | Embedded vector store | — | ✅ *(two trees)* | ✅ | [§5](#5-break-2--the-shared-vector-store) |
| 3 | Handshake routing | — | — | ✅ | [§6](#6-break-3--the-sticky-handshake) |
| 4 | In-flight runs — drain | ✅ | ✅ | ✅ | [§7](#7-break-4--the-drain-and-the-sweep) |
| 4 | In-flight runs — sweep | ✅ | ✅ | ✅ | [§7](#7-break-4--the-drain-and-the-sweep) |
| 4 | Grace period, rollouts | — | — | ✅ | [§7](#7-break-4--the-drain-and-the-sweep) |
| 5 | Message positions | ✅ | ✅ | ✅ | [§8](#8-break-5--two-writers-one-position) |
| 6 | Broadcasting | code check | ✅ *(when it exists)* | ✅ | [§9](#9-break-6--broadcasting-before-the-feature-exists) |
| 10 | Postgres pool | ✅ | ✅ | ✅ | [§10](#10-postgres-connections) |
| 12 | Summary leases | per-process | ✅ | ✅ | [§11](#11-rolling-summaries-and-the-lease) |
| 6/7 | Housekeeping + schema locks | — | ✅ | ✅ | [§12](#12-startup-housekeeping-and-the-schema-race) |
| 11 | Redis fail-soft | ✅ | ✅ | ✅ | [§13](#13-redis--proving-it-fails-soft) |
| 11 | Cache ordering | — | ✅ | ✅ | [§14](#14-ordering-and-the-week-old-pod) |

The numbers are the rows of [SCALING.md §1](SCALING.md#1-the-verdict-table), so
a failure here has a paragraph there explaining itself.

### The problem behind each row, in plain English

One block per row of the matrix above, same numbers, always the same four
parts:

- **Complaint** — what a user would actually say when this breaks.
- **What is going on** — the mechanism, in short sentences.
- **The picture** — usually two: what happens *without* the handling, and what
  happens *with* it.
- **Example** — one walk-through with real numbers, log lines or SQL, so you
  know what you are looking at.

Then one line for the fix, and whether it is yours to do or already handled.

---

#### 1 · JWT signing key

**Complaint:** *"It logs me out at random. I sign in again and it works. Then it
happens again."*

**What is going on.** Signing in gives the browser a token with a stamp on it.
Only something holding the signing key can make that stamp, or check it. When
`JWT_SECRET_KEY` is not set, each instance invents its own key while it boots —
a different one per pod, and a new one every restart.

So Pod A's stamp means nothing to Pod B. And the browser's plan B for a
rejected token — spend the refresh token, get a new one — fails the same way,
because that refresh token carries the same unrecognised stamp. Two rejections
in a row is what the client treats as "signed out".

**Without a shared key:**

```mermaid
sequenceDiagram
  autonumber
  participant U as Browser
  participant A as Pod A, key a1b2
  participant B as Pod B, key z9y8
  U->>A: sign in
  A-->>U: token stamped a1b2
  Note over U: the browser has no idea<br/>a second pod exists
  U->>B: next request, same token
  B->>B: check the stamp against z9y8 — no match
  B-->>U: 401
  U->>B: plan B, spend the refresh token
  B-->>U: 401, that one is stamped a1b2 as well
  Note over U,B: two rejections = the login page
```

**With one key everywhere:** every pod checks against the same stamp, so it
does not matter which one answers. Nothing else changes.

**Example.** Four pods, no shared key. Each request has a one-in-four chance of
landing back on the pod that minted the token, so **three of every four fail**.
It is never broken twice in a row and never reproducible on demand, which is
exactly why it gets filed as "flaky login" instead of as an outage.

By hand, on two local instances with the key unset:

```
A 200      <- the pod that signed the token
B 401      <- any other pod
```

And at startup, before anyone has logged in at all:

```
WARNING  main  Tokens are signed with a key that dies with this process…
```

**Fix:** the same `JWT_SECRET_KEY` on every instance, from one Secret.
**Yours.**
→ [§4](#4-break-1--the-shared-signing-key)

---

#### 2 · Embedded vector store

**Complaint:** *"It answered my question, but it clearly has not read the filing
I just uploaded."*

**What is going on.** Uploading a filing means splitting it into chunks,
turning each into a vector, and storing them in Chroma. Embedded Chroma is a
folder on the disk of the pod that did the work — nothing else can see it.

Asking a question means searching those vectors and handing the best chunks to
the model. If the question lands on a pod that never received the upload, the
search finds nothing.

Here is the cruel part: **finding nothing is a legitimate outcome.** A question
about something the filing does not cover also retrieves nothing. So there is no
error path to hit — the model answers from general knowledge, in the same
confident voice, and nobody can tell from the screen.

**Without a shared store:**

```mermaid
flowchart TB
  U["09:00 — analyst uploads apple-10k.pdf"] --> A["Pod A chunks and embeds it"]
  A --> DA[("disk on Pod A<br/>412 chunks")]
  Q["09:01 — what were the research costs?"] --> LB{"the balancer picks a pod"}
  LB -->|"lands on Pod A"| GOOD["searches 412 chunks<br/>answer quotes the filing"]
  LB -->|"lands on Pod B"| DB[("disk on Pod B<br/>0 chunks, it never saw the upload")]
  DB --> BAD["Retrieved 0 chunk(s)<br/>a fluent answer with no filing behind it"]
```

**With a shared Chroma server:** both pods write to and read from the same
store, so it stops mattering which one takes the upload and which takes the
question.

**Example.** Two pods, so roughly **half** of all questions about a freshly
uploaded filing are answered from nothing. Four pods and it is three quarters.

Upload through A, ask through B, and read B's log:

```
INFO  …vector_store  Retrieved 0 chunk(s) for …
```

The answer that comes back will contain no figure that appears in the document —
that is the tell, and it is the only one. This is why it is the most important
test in the file: everything else fails loudly, and this one fails by looking
like success.

**Fix:** one Chroma server every instance points at (`CHROMA_HOST`), plus the
init container so a pod that cannot reach it never serves traffic. **Yours.**
→ [§5](#5-break-2--the-shared-vector-store)

---

#### 3 · Handshake routing

**Complaint:** *"The page connects, then drops, then connects again. Forever."*

**What is going on.** A WebSocket is not always available, so Socket.IO opens
with plain HTTP polling: one request to start a session, then more requests that
refer back to it by its id (`sid`).

That `sid` lives in the memory of the process that answered the first request.
It is not in Postgres and not in Redis — there is nowhere else to look it up.

A round-robin balancer sends request two to a different pod, which has never
heard of the sid and says so. The client does the correct thing — starts a fresh
handshake — and gets split again. That is the loop.

**Without stickiness:**

```mermaid
sequenceDiagram
  autonumber
  participant C as Browser
  participant LB as Load balancer
  participant A as Pod A
  participant B as Pod B
  C->>LB: start a session
  LB->>A: (round robin)
  A-->>C: you are sid-1
  C->>LB: continue sid-1
  LB->>B: (round robin, other pod this time)
  B-->>C: unknown session
  C->>LB: start a session again…
  Note over C,B: same coin toss, same result
```

**With a sticky cookie:** the first response sets a cookie naming the pod, the
balancer reads it on every later request, and all four requests land on Pod A.

```mermaid
flowchart LR
  R1["request 1"] --> LB{"balancer"} --> A["Pod A<br/>holds sid-1"]
  A --> CK["set-cookie: cfa-sticky=pod-a"]
  CK --> R2["requests 2, 3, 4 carry the cookie"] --> LB
```

**Example.** Two pods means about **half** of all handshakes fail, so the app
"works if you refresh a couple of times". In the browser's network tab you can
watch it: a new `sid` every second or two, none of them lasting.

The single fact to check first:

```
set-cookie: cfa-sticky=…      <- on the handshake response
```

No cookie, no stickiness, and nothing you change in the application can help —
the routing decision has already been made by the time the request arrives.

**Fix:** cookie affinity on the ingress. **Yours.**
→ [§6](#6-break-3--the-sticky-handshake)

---

#### 4 · In-flight runs — the drain

**Complaint:** *"I asked a question, the answer started appearing, then it just
stopped. It is still spinning."*

**What is going on.** One question is **two** writes to the ledger, with a long
gap between them:

1. The **question** row is written straight away, so the dossier shows it.
2. The model runs — 30 to 90 seconds is normal for a filing summary.
3. The **answer** row is written when the stream finishes.

A rollout sends `SIGTERM` in the middle of step 2. Without a drain the process
exits immediately, and the ledger is left with a question and nothing after it.
No retry exists: the run lived in that process's memory, and the browser waiting
for it has no way to ask again on its own.

The handling is `drain()` in [api/socket.py](../backend/Analyzer/api/socket.py):
new work has already stopped arriving, so shutdown waits up to
`SHUTDOWN_DRAIN_SECONDS` (120) for the runs it is already holding.

**Without the drain:**

```mermaid
sequenceDiagram
  autonumber
  participant U as Analyst
  participant P as Pod
  participant DB as Postgres
  U->>P: summarise the risk factors
  P->>DB: write the QUESTION, seq 7
  Note over P: model runs, needs 60s
  P-->>U: tokens streaming…
  Note over P: SIGTERM at second 20 — exits at once
  P--xU: stream stops mid-sentence
  Note over DB: no ANSWER row is ever written<br/>seq 7 sits there with nothing after it
```

**With the drain:**

```mermaid
sequenceDiagram
  autonumber
  participant U as Analyst
  participant P as Pod
  participant DB as Postgres
  Note over P: SIGTERM at second 20
  P->>P: stop accepting new work
  Note over P: Waiting up to 120s for 1 run(s) to finish
  P-->>U: the rest of the answer streams normally
  P->>DB: write the ANSWER, seq 8
  Note over P: All 1 in-flight run(s) finished — then exits at 60s
```

**Example.** Ask for something long, then `kubectl rollout restart` while it is
still streaming. Watch the leaving pod's log. A pass is **both** of these lines:

```
INFO  api.socket  Waiting up to 120s for 1 run(s) to finish
INFO  api.socket  All 1 in-flight run(s) finished
```

A fail is this one, which tells you exactly what it cost:

```
WARNING  api.socket  1 run(s) did not finish in time — their answers are lost…
```

Then check the ledger itself, because the log is a claim and the ledger is the
evidence — every question row must be followed by an answer row:

```sql
SELECT seq, role, status FROM messages
WHERE conversation_id = '…' ORDER BY seq;
```

Scale makes it routine rather than rare: a rollout across three pods drains
three times, so *every* answer in flight anywhere in the deployment is at stake
on every deploy. At one instance you only paid this at restarts.

**Fix:** wait for in-flight runs on shutdown. **Handled** — the test is that the
wait really happens. → [§7](#7-break-4--the-drain-and-the-sweep)

---

#### 4 · In-flight runs — the sweep

**Complaint:** *"That question from Tuesday is still thinking."*

**What is going on.** The drain only helps when the process is *asked* to stop.
Some deaths give no warning at all — a `SIGKILL` past the grace period, an OOM
kill, a node that vanishes. No code runs, so nothing is written.

The wreckage looks exactly like the drain failure: a question row with nothing
after it. Nobody is coming back for it — the browser that asked is long closed.

So at startup, one instance goes looking for that shape and writes the answer
that never came: *"This run was interrupted before it finished…"*, with
`status = error`, so the analyst gets something they can act on instead of a
spinner.

The hard part is telling the two apart. **A question being answered right now on
another pod looks identical** — a question row, nothing after it yet. Mark that
one and you have faked an error on a live run, which is worse than the problem.
Two things prevent it: only questions older than `STALE_RUN_MINUTES` (30) are
considered, and each one is re-checked under the conversation's row lock
immediately before writing.

```mermaid
flowchart TB
  K["pod dies with no warning<br/>SIGKILL, OOM, node lost"] --> L["the ledger is left like this:<br/>seq 7 question — written<br/>seq 8 answer — never written"]
  L --> S["the next instance starts and sweeps"]
  S --> C{"is that question<br/>older than 30 minutes?"}
  C -->|"no — another pod<br/>could still be answering it"| SKIP["leave it alone<br/>marking a live run would fake an error"]
  C -->|"yes — no run lives that long"| W["write the answer row:<br/>interrupted by a restart, status = error"]
  W --> R["the analyst sees an error and can ask again,<br/>instead of a spinner that never ends"]
```

**Example.** Strand a run on purpose: force-kill the process mid-answer with no
`SIGTERM` at all, so nothing drains. Then back-date the question so you do not
have to wait half an hour for the cutoff:

```sql
UPDATE messages SET created_at = now() - interval '45 minutes'
WHERE conversation_id = '…' AND seq = 7;
```

Restart, and the startup log says:

```
INFO  conversations.service  Closed out 1 interrupted run(s) from a previous life
```

Run the negative too, because it is the half that can hurt you: while instance B
is streaming a long answer, restart instance A. B's live run must **not** be
swept — the question is still unanswered when A comes back up, and B finishes it
normally.

**Fix:** close out abandoned runs at startup, leaving live ones alone.
**Handled** — that cutoff is the whole difficulty.
→ [§7c](#7c-the-sweep)

---

#### 4 · Grace period

**Complaint:** *"The drain is right there in the logs, and the answer is still
gone."*

**What is going on.** Two clocks start at `SIGTERM`, and they do not know about
each other:

- **Yours:** the app drains, up to `SHUTDOWN_DRAIN_SECONDS` (120).
- **Kubernetes':** `terminationGracePeriodSeconds`, **30 by default**. When it
  fires, it sends `SIGKILL`. There is no negotiation and no extension.

If the platform's clock is the shorter one, the drain is theatre. Your logs show
you waiting patiently, the pod is killed halfway through anyway, and the answer
is lost exactly as it was before — while every line you added to prevent it says
it is working.

```mermaid
flowchart TB
  subgraph bad["grace 30s — the drain cannot win"]
    B0["0s SIGTERM"] --> B1["draining… the answer needs 60s"]
    B1 --> B2["30s SIGKILL, no warning"]
    B2 --> B3["answer lost anyway<br/>and the log makes it look handled"]
  end
  subgraph good["grace 180s — the drain wins"]
    G0["0s SIGTERM"] --> G1["draining…"]
    G1 --> G2["60s answer written, pod exits on its own"]
    G2 --> G3["120s of headroom never used"]
  end
```

**Example.** The tell is a stopwatch, not a log line. Watch a pod leave during a
rollout:

```bash
kubectl -n cfa get pod -w
```

A pod that exits at **exactly** the same second every time — 30.0s, over and
over — was killed. A pod that exits at 41s once and 58s the next time finished
its work and left. *At* the deadline means killed; *before* it means drained.

Rule of thumb: grace period above the drain budget with room to spare — drain
120 → grace 150 or 180.

**Fix:** `terminationGracePeriodSeconds` comfortably above
`SHUTDOWN_DRAIN_SECONDS`. **Yours — the manifest.**
→ [§7b](#7b-the-grace-period-rig-3)

---

#### 5 · Two writers, one message position

**Complaint:** *"The answer was on my screen. I refreshed and it was gone."*

**What is going on.** Messages are numbered per dossier: 1, 2, 3… A unique
constraint, `uq_message_position` on (conversation, seq), makes two rows with
the same number impossible.

Writing a message is three steps: **read the highest number, add one, insert.**
Two writers doing that at the same instant both read 4, and both try to write 5.
One wins. The loser gets an `IntegrityError`.

That error is then **caught on purpose** — `_record_answer` will not fail a
request over bookkeeping. So there is no 500, no error toast, and nothing in the
log at INFO. The analyst watched the answer stream in token by token, and it is
simply not there afterwards.

The fix is to make "read the highest, write the next" one indivisible step:
lock the conversation row first (`SELECT … FOR UPDATE`). The lock lives in
Postgres, so it works across processes, not just across tasks in one.

**Without the row lock:**

```mermaid
sequenceDiagram
  autonumber
  participant A as Writer A
  participant DB as Postgres
  participant B as Writer B
  A->>DB: highest seq here? → 4
  B->>DB: highest seq here? → 4
  A->>DB: INSERT seq 5 — accepted
  B->>DB: INSERT seq 5 — rejected, uq_message_position
  Note over B: the error is swallowed on purpose<br/>the answer is simply gone
```

**With the row lock:**

```mermaid
sequenceDiagram
  autonumber
  participant A as Writer A
  participant DB as Postgres
  participant B as Writer B
  A->>DB: lock the conversation row
  B->>DB: lock the conversation row — waits its turn
  A->>DB: highest 4, INSERT seq 5, commit
  DB-->>B: your turn now
  B->>DB: highest 5, INSERT seq 6, commit
```

**Example.** This one needs **no second instance at all** — it is older than any
thought of scaling. Open the same dossier in two browser tabs and ask a question
in both at the same moment. Then count:

```sql
SELECT count(*) AS rows, max(seq) AS highest FROM messages
WHERE conversation_id = '…';
```

They must be equal. `rows = 7, highest = 8` means one row lost the race, and
that missing row is somebody's answer. The second check is the shape of the
ledger: every `user` row must be followed by an `assistant` row.

More instances do not create this bug. They only make it ordinary — a filing
upload racing a question is enough, on any number of pods.

**Fix:** lock the conversation row while a position is claimed. **Handled** —
verify by counting. → [§8](#8-break-5--two-writers-one-position)

---

#### 6 · Broadcasting

**Complaint:** none today — and that *is* the finding. This row is here so it
stays that way.

**What is going on.** Socket.IO can send an event two ways: to **one
connection** (`to=sid`), or to a **room** — a name that any number of
connections can join.

Room membership lives in the memory of one process. A pod can only reach the
connections it is personally holding.

Every emit in this app today names a sid: tokens, errors, the done event, all of
it goes back to the browser that asked. So nothing ever needs to cross from one
pod to another, and the app is correct at any number of instances with no
configuration at all.

The day someone adds a shared dossier, a "someone is typing" dot, or a live
notification, that stops being true — quietly. The feature works perfectly in
testing, because your two tabs happened to land on the same pod.

```mermaid
flowchart TB
  subgraph now["today, every emit names one connection"]
    E1["Pod A: emit to=sid"] --> C1["that same browser — always correct"]
    N1["no other pod needs to know anything"]
  end
  subgraph later["the day someone emits to a room"]
    E2["Pod A: emit to room dossier-42"] --> V1["viewer connected to Pod A — sees it"]
    E2 -.->|"never arrives"| V2["viewer connected to Pod B — sees nothing<br/>Pod B holds that connection, Pod A cannot reach it"]
    FIX["set SOCKETIO_MESSAGE_QUEUE_URL<br/>pods relay events to each other through Redis"] --> V2
  end
```

**Example.** The whole test is one command, and today it prints nothing:

```bash
grep -rn "sio.emit" backend/Analyzer | grep -v "to=sid"
```

The day it prints a line, this row changes from "nothing to do" to "set the
message queue". Two viewers, two pods, and one of them never hears about the
update: **half your viewers, silently.**

**Fix:** keep every emit addressed to a sid; set `SOCKETIO_MESSAGE_QUEUE_URL`
when that stops being possible. **Not yet — the test is confirming it is still
not yet.** → [§9](#9-break-6--broadcasting-before-the-feature-exists)

---

#### 10 · Postgres connections

**Complaint:** *"FATAL: sorry, too many clients already"* — and it hits
everybody at once, including your own `psql`.

**What is going on.** Each instance keeps a pool of database connections: 5 held
open (`DB_POOL_SIZE`), plus up to 10 more under load (`DB_MAX_OVERFLOW`). **15
per instance, at the worst moment.**

Postgres has one `max_connections` for the whole server — often 100 by default.

So the ceiling is reached by the *size of the deployment*, not by how busy it
is. Scale from 4 pods to 8 on a quiet afternoon and you can exhaust it having
served nothing. And the failure lands on whoever connects next, which is rarely
the thing that caused it: a health check, a migration, you.

```mermaid
flowchart TB
  P1["Pod 1 — up to 15"] --> T
  P2["Pod 2 — up to 15"] --> T
  P3["…"] --> T
  P8["Pod 8 — up to 15"] --> T
  T["8 x 15 = 120 wanted"] --> PG{"max_connections = 100"}
  PG -->|"first 100"| OK["served normally"]
  PG -->|"everything after"| X["FATAL: sorry, too many clients already<br/>hits a health check, a migration, your psql"]
```

**Example.** Do the arithmetic before the deployment does it for you:

```
pods x (DB_POOL_SIZE + DB_MAX_OVERFLOW)  +  room for migrations and psql
  8  x (      5      +       10       )  +  ~10   =  130   >  100   ✗
  4  x (      5      +       10       )  +  ~10   =   70   <  100   ✓
```

Then watch the real number while the tests run:

```sql
SELECT count(*) FROM pg_stat_activity;
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
```

The second query is the one with the trap in it. Any `idle in transaction`
during a summary fold means a pooled connection is being **held across a model
call** — 60 seconds of a connection doing nothing but occupying a slot. That
turns headroom that exists on paper into exhaustion in practice.

**Fix:** size the pools so N instances fit, and never hold a connection across a
model call. **Yours.** → [§10](#10-postgres-connections)

---

#### 12 · Rolling summaries

**Complaint:** two identical "summarised" lines in the log, a minute of model
time spent twice.

**What is going on.** Long dossiers get folded: older turns are replaced by a
rolling summary so the prompt stays a sane size. It triggers when the message
count crosses `HISTORY_SUMMARY_THRESHOLD` (24).

Both instances see the same count at the same moment, so both can decide to
fold the same dossier at once.

The important thing is **what that costs**, and the answer is: one wasted call
to the model, and nothing else. The fold writes `summary_through_seq`, which may
only move forwards — a slow fold that lands after a newer one is discarded
rather than applied. Two folds produce one correct summary.

That is why the coordination here is a **lease in Redis** and not a lock in
Postgres. A lease is allowed to be wrong occasionally, and when Redis is absent
it simply says yes to everybody. Nothing that must be correct is built on it.

```mermaid
flowchart TB
  T["message 24 lands — time to fold"] --> A["Pod A claims the Redis lease — got it"]
  T --> B["Pod B tries the same lease — already taken"]
  A --> M["one model call, about 30s"]
  B --> SK["skips, gets on with serving"]
  M --> W["writes summary_through_seq = 20"]
  NR["no Redis? the lease says yes to everyone"] --> D["both pods fold<br/>one wasted call, same result"]
  W --> G["summary_through_seq may only move forwards<br/>a stale fold that lands late is discarded"]
  D --> G
```

**Example.** Make folds happen in minutes rather than hours:

```bash
kubectl -n cfa set env deploy/cfa-backend HISTORY_SUMMARY_THRESHOLD=4
```

Ask six short questions in one dossier. Across **both** pods' logs there should
be exactly one line per crossing:

```
INFO  conversations.service  Summarised … through seq 6 (4 message(s) folded)
```

Now the negative, which is the interesting half: stop Redis, restart both, and
both pods fold. Check the column anyway —

```sql
SELECT summary_through_seq, summary_tokens FROM conversations WHERE id = '…';
```

— and it still only moves forwards. You paid for one extra model call and got a
correct summary, which is the whole argument for a best-effort lease.

**Fix:** a best-effort Redis lease, plus forward-only ordering that makes a
duplicate harmless. **Handled.**
→ [§11](#11-rolling-summaries-and-the-lease)

---

#### 6/7 · Housekeeping and the schema race

**Complaint:** *"First deploy of the day and one pod is in CrashLoopBackOff."*

**What is going on.** Some work at boot is "once per deployment", not "once per
pod": pruning filings nothing points at, and sweeping interrupted runs. Every
pod doing it is wasteful — a lot of duplicate scanning at exactly the busiest
moment in a pod's life.

One of them is worse than wasteful. On a fresh database, two pods calling
`create_all` at the same instant both issue `CREATE TABLE`. One gets
`DuplicateTable` and exits, Kubernetes restarts it, and it can happen again.
That is a crash loop caused by nothing but two pods starting together.

Both jobs are wrapped in a **Postgres advisory lock**. One pod does the work;
the others skip it and say so in the log. An advisory lock rather than a Redis
lease, because unlike the summary fold, this one has to be right every time.

```mermaid
flowchart TB
  S["Pod A and Pod B boot in the same second"] --> L{"advisory lock — exactly one winner"}
  L -->|"Pod A wins"| W1["create the schema"]
  W1 --> W2["prune orphaned filings"]
  W2 --> W3["sweep interrupted runs"]
  W3 --> R["releases the lock"]
  L -->|"Pod B loses"| SK["logs 'another instance is doing the startup housekeeping'<br/>and starts serving immediately — it does not wait"]
  NL["without the lock"] -.-> XX["both run CREATE TABLE<br/>one exits with DuplicateTable → CrashLoopBackOff"]
```

**Example.** On a **scratch** database, drop everything and start both pods
together:

```sql
DROP SCHEMA public CASCADE; CREATE SCHEMA public;
```

Pass: both pods reach Ready, no `DuplicateTable` anywhere, and across the two
logs exactly one line —

```
INFO  main  Another instance is doing the startup housekeeping
```

One is the number to check. Zero means both pods thought they were the winner
and the lock is not doing anything; two means nobody did the work at all.

**Fix:** an advisory lock around both jobs. **Handled** — test the pod that
skips as carefully as the one that wins.
→ [§12](#12-startup-housekeeping-and-the-schema-race)

---

#### 11 · Redis fail-soft

**Complaint:** there should not be one. That is the entire test.

**What is going on.** Redis holds two things here: the cached tail of each
conversation, and the summary leases above. **Neither is the truth** — Postgres
is. Everything in Redis can be rebuilt by reading the database.

So the risk is not losing the cache. The risk is the **retry**. If the app tried
to reconnect on every request, every request would sit through a connection
timeout, and a cache outage would have become a site outage — the classic way a
soft dependency turns hard.

Instead each subsystem switches itself off at the first error and stays off
until the process restarts. The cost is one extra `SELECT` per question.

```mermaid
flowchart TB
  Q["a question arrives"] --> C{"is the cache switched on?"}
  C -->|"yes"| H["read the tail from Redis — fast"]
  C -->|"no"| P["read it from Postgres, ORDER BY seq<br/>same answer, milliseconds slower"]
  E["Redis goes away"] --> F["the first error switches the cache off<br/>and it stops trying"]
  F --> C
  RB["Redis comes back"] -.-> ST["still off, on purpose<br/>restart the backends or you will<br/>conclude the cache is broken"]
```

**Example.** Take it away completely and run the whole analyst path — sign in,
upload, ask, reload, read the history back:

```bash
kubectl -n cfa scale deploy/redis --replicas=0
```

All of it must work. Two warnings, once each, are the receipt:

```
WARNING  conversations.cache  Message cache disabled after a Redis error: …
WARNING  core.leases          Leases disabled after a Redis error: …
```

Then remember the consequence, because it is the thing people get wrong:
bringing Redis back does **not** bring the cache back.

```bash
kubectl -n cfa scale deploy/redis --replicas=1
kubectl -n cfa rollout restart deploy/cfa-backend   # <- without this it stays off
```

**Fix:** nothing to do. **Handled** — a deployment that *needs* Redis to be
correct has a bug in it. → [§13](#13-redis--proving-it-fails-soft)

---

#### 11 · Cache ordering

**Complaint:** *"The conversation is in the wrong order — but only sometimes,
and it fixes itself."*

**What is going on.** The cached tail is a list per dossier. Each instance
appends to it immediately after writing its message to Postgres.

Two instances writing the same dossier are two independent racers between
Postgres and Redis. Pod B can win the second leg after losing the first, so
`seq 4` lands in the list before `seq 3`. **The list itself is genuinely out of
order** — that is not prevented, and it does not need to be.

What prevents anyone ever seeing it is the read path: it sorts by `seq` before
answering. So the property worth testing is not "the cache is in order" but
**"the cache and the database always give the same answer"**.

```mermaid
flowchart TB
  A["Pod A writes seq 3 to Postgres"] --> RA["appends 3 to the cached tail"]
  B["Pod B writes seq 4 to Postgres"] --> RB["appends 4 to the cached tail"]
  RB --> L[("cached tail as stored:<br/>4, then 3 — out of order")]
  RA --> L
  L --> S["the read path sorts by seq"]
  PGDB[("Postgres, ORDER BY seq:<br/>3, then 4")] --> S
  S --> O["3, then 4 — from either source, every time"]
```

**Example.** Read the same dossier from both instances, then flush the cache and
read a third time:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/api/conversations/$SID/messages
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18002/api/conversations/$SID/messages
kubectl -n cfa exec deploy/redis -- redis-cli FLUSHALL
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/api/conversations/$SID/messages
```

All three responses identical, in `seq` order; only the last is slower. If the
cached answer and the flushed answer differ, the cache is authoritative
somewhere it should not be, and that is a real bug — not a cosmetic one.

**Fix:** sort by `seq` on read. **Handled** — cache and database agreeing is the
property. → [§14](#14-ordering-and-the-week-old-pod)

---

**The short version.** Five of these are **yours**, and they are all
configuration rather than code: the shared signing key (1), the shared vector
store (2), the sticky ingress (3), the grace period (4), and the connection
arithmetic (10). Nothing in the application can decide them, and each one is
something a deployment can still get wrong today.

The rest are already handled. What you are testing there is that they *stay*
handled — which is why every one of them has a negative to run first: take the
handling away, watch it fail, put it back.

---

## 3. Ground rules

Six habits, each of which exists because skipping it produces a test that passes
for the wrong reason.

1. **Address instances directly.** Anything that compares two instances must
   reach them individually — two ports on Rig 2, two `port-forward`s on Rig 3.
   Through a sticky ingress, both halves of a split test land on one pod and it
   passes without having tested anything.
2. **Do the negative first.** Take the setting away, watch the failure, put it
   back, watch it pass. In that order you learn what the failure *looks like*,
   which is what you will actually be recognising at 3am.
3. **One variable.** Each test below changes one setting. Two at once and a pass
   tells you nothing about which one mattered.
4. **Read every instance's log, from its first line.** `--tail=-1` and
   `--prefix` on Rig 3; the startup lines that matter most scroll away in
   seconds and never repeat.
5. **Use a scratch database.** These tests create accounts, write ledgers, and a
   couple of them (`DROP SCHEMA`, back-dating rows) destroy data on purpose.
6. **Rule the model out first.** A surprising number of "scaling failures" are
   Ollama being down or slow. `curl -s localhost:11434/api/tags` before you
   start believing anything else.

Every test that follows has the same beats: **what it proves**, **the rig**,
**break it**, **watch**, **put it back**.

---

## 4. Break #1 — the shared signing key

**Proves:** a token minted by one instance is accepted by every other, and that
a deployment says so at startup rather than at somebody's first login.
**Rig:** 2 or 3. **Time:** two minutes.

### The negative

Start both instances with `JWT_SECRET_KEY` unset — the default, and what a
deployment that forgot the Secret looks like:

```bash
# Rig 2: drop JWT_SECRET_KEY from both terminals and restart them
# Rig 3: kubectl -n cfa set env deploy/cfa-backend JWT_SECRET_KEY=""
```

Mint a token on A and spend it on B:

```bash
TOKEN=$(curl -s -X POST localhost:8001/api/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"scale-1@example.com","name":"Scale One","password":"testpassword"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -s -o /dev/null -w 'A %{http_code}\n' localhost:8001/api/auth/me -H "Authorization: Bearer $TOKEN"
curl -s -o /dev/null -w 'B %{http_code}\n' localhost:8002/api/auth/me -H "Authorization: Bearer $TOKEN"
```

```
A 200
B 401
```

That 401 is Break #1 with nothing else in the way. Behind a load balancer it is
not a clean failure, it is a *coin toss* — roughly `(N-1)/N` of requests — which
in a browser reads as being signed out at random.

**The refresh path fails with it**, and that is why the symptom is a logout
rather than a hiccup: the client answers a 401 by spending its refresh token,
and that token was signed with the same dead key.

```bash
REFRESH=$(curl -s -X POST localhost:8001/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"scale-1@example.com","password":"testpassword"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["refresh_token"])')

curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/api/auth/refresh \
  -H 'content-type: application/json' -d "{\"refresh_token\":\"$REFRESH\"}"   # 401
```

**The socket half.** Point the workbench at B (`window.__BACKEND_URL__` in
[frontend/config.js](../frontend/config.js)) while signed in through A. The
handshake is refused and B says so:

```
INFO  api.socket  Handshake from <sid> refused — Token is not valid.
```

### The positive

Restart both with the *same* key and repeat: `A 200`, `B 200`, and the socket
connects against either instance.

### The startup line, which is the check you actually keep

```bash
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 --prefix \
  | grep -c "JWT_SECRET_KEY is not set"
```

Zero. Any hits at all and authentication is already unreliable. The line appears
in the first seconds of a pod's life on purpose (`main._check_signing_key`
provokes it) — before that change it only appeared at the first login, which
could be hours after anyone was reading the rollout.

### The single-instance version, in ten seconds

Rig 1: sign in, restart the process, reload the page. Signed out means the key
is ephemeral. Still signed in means it is set. One instance survives this bug at
the price of signing everybody out on every deploy; two instances do not survive
it at all.

---

## 5. Break #2 — the shared vector store

**Proves:** a filing ingested through one instance answers questions asked on
another. **Rig:** 3, or Rig 2 with a second tree. **Time:** five minutes.

This is the test that matters most, because it is the only one that proves two
instances share *one* store rather than each merely having one.

### The split, by hand

Upload through A, ask through B, never the reverse and never both on one.

```bash
SID=split-$(date +%s)          # a dossier id, the same one the client would send

curl -s -X POST http://127.0.0.1:18001/api/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "session_id=$SID" -F "file=@mock_10k_filing.txt"
```

A logs the ingest:

```
INFO  …vector_store  Ingested mock_10k_filing.txt -> 3 chunks (chat=…)
```

Now ask in that dossier **on B**. The upload opened the dossier row, so it is
already in the sidebar: point the workbench at B, reload, open the dossier the
upload created, and ask something only the filing can answer ("what does this
filing say about revenue?").

| | What you see | What it means |
| --- | --- | --- |
| **Pass** | an answer citing the filing; B logs `Retrieved 3 chunk(s) …` | one store, two instances |
| **Fail** | "No filing is attached to this dossier yet." | B searched its own private store and found nothing |

The failure is nastier than it sounds: the dock still lists the filing, because
the row is in Postgres and only the chunks are missing. A filing that exists and
answers nothing is the afternoon-wasting shape of this bug.

### The line to check before trusting any deployment

```bash
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 --prefix \
  | grep "VectorService ready"
```

```
INFO  …vector_store  VectorService ready (store=chroma:8000, shared=True, …)
```

Every instance, `shared=True`. One `shared=False` among them is not a warning
about the bug — it *is* the bug, and every other check can still pass.

### Break it on purpose

```bash
kubectl -n cfa set env deploy/cfa-backend CHROMA_HOST=""
kubectl -n cfa rollout status deploy/cfa-backend
```

Both pods come up `shared=False`, each with its own disk, and the split test
fails on exactly the step that names the cause. Undo:

```bash
kubectl -n cfa set env deploy/cfa-backend CHROMA_HOST-
```

**Filings do not migrate when you flip this.** After switching either way,
re-upload before you conclude anything — otherwise you are testing a store that
never had the filing in it.

### The startup gate

A store you cannot reach should stop the rollout, not degrade the service:

```bash
kubectl -n cfa scale deploy/chroma --replicas=0
kubectl -n cfa rollout restart deploy/cfa-backend
```

Pass: new pods sit in `Init:1/2`, logging `waiting for chroma at
http://chroma:8000/api/v2/heartbeat` every two seconds, while the old pods keep
serving (`maxUnavailable: 0`). A pod that starts anyway and serves failing
queries one at a time is the regression this gate exists to prevent. Scale
Chroma back and the rollout completes on its own.

### The ordering window, worth testing once

Startup housekeeping drops collections no conversation claims, and upload opens
the conversation row *before* ingesting for exactly that reason. To confirm the
window is closed: start a large upload through A and, while it is still running,
restart B so its housekeeping runs. Pass: the filing survives and answers. Fail
(the old order): the collection is dropped out from under the upload, and the
filing is listed but empty — Break #2's symptom from a completely different
cause, which is why it is worth being able to tell them apart.

---

## 6. Break #3 — the sticky handshake

**Proves:** a polling handshake stays on one instance. **Rig:** 3 only — the
ingress is the thing under test, not the app. **Time:** five minutes.

### Step 1 — the cookie exists

```bash
curl -si 'http://cfa.local/socket.io/?EIO=4&transport=polling' | grep -i set-cookie
```

```
set-cookie: cfa-sticky=1730…; Path=/; HttpOnly
```

Nothing there means nothing is pinned. Check the affinity annotations on the
Ingress and — the mistake that actually happens — that `/socket.io` routes
straight to the `backend` Service rather than through the frontend pod, which
would put kube-proxy between the cookie and the pods.

### Step 2 — the cookie is obeyed

Repeat a handshake ten times carrying that cookie, then see where they landed:

```bash
COOKIE=$(curl -si 'http://cfa.local/socket.io/?EIO=4&transport=polling' \
         | sed -n 's/^[Ss]et-[Cc]ookie: \(cfa-sticky=[^;]*\).*/\1/p')

for i in $(seq 10); do
  curl -s -o /dev/null -H "Cookie: $COOKIE" 'http://cfa.local/socket.io/?EIO=4&transport=polling'
done

kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --since=2m --prefix \
  | grep 'refused — no token' | awk '{print $1}' | sort | uniq -c
```

All ten lines from one pod is a pass. Split across two is a deployment where
every polling client will loop. The handshakes are refused for having no token,
which is fine — what is being tested is *where* they land, not whether they
succeed.

### Step 3 — the real thing, in a browser

The client asks for `["websocket", "polling"]` at
[app.js:127](../frontend/app.js#L127). To force the path that actually breaks,
change that array to `["polling"]`, rebuild the frontend image, restart it:

```bash
minikube image build -t cfa-frontend:latest frontend
kubectl -n cfa rollout restart deploy/cfa-frontend
```

Watch the network tab. Pass: a steady sequence of polling requests carrying one
`sid`. Fail: connect / disconnect / connect, because each poll is reaching a pod
that never issued that `sid`.

### The negative

```bash
kubectl -n cfa annotate ingress cfa nginx.ingress.kubernetes.io/affinity-
```

With polling forced, the loop starts within a few requests — and this is the one
break whose symptom is visible without reading a log. Put it back by reapplying
the manifest, which restores all four affinity annotations rather than the one
you removed:

```bash
kubectl apply -k deploy/minikube
```

### Why a WebSocket test is not a test

An upgraded WebSocket is one TCP connection and cannot split, so a
WebSocket-only test passes against a deployment with no stickiness whatsoever.
The users it lies about are the ones behind proxies that refuse upgrades — the
ones you cannot see from your desk. Forcing polling is the whole test.

### While you are here — reconnection

Delete the pod your browser is pinned to. Pass: the client reconnects on its own
(20 attempts, 1.5s apart) onto the surviving pod, a "Reconnected" toast appears,
and the next question works. This is the behaviour that makes a rollout cost a
reconnect instead of an outage.

---

## 7. Break #4 — the drain and the sweep

Two mechanisms, for two different deaths. Test them separately or you will not
know which one saved you.

```mermaid
flowchart TD
  T["SIGTERM — rollout, drain, scale-down"] --> D["the pod waits for its in-flight runs<br/>SHUTDOWN_DRAIN_SECONDS (120)"]
  D --> W["the answer row is written"]
  K["SIGKILL — past the grace period, or a lost node"] --> N["nothing is written"]
  N --> S["the next instance to start sweeps:<br/>a question older than STALE_RUN_MINUTES<br/>with nothing after it → an error row"]
```

### 7a. The drain

**Proves:** a shutdown finishes the answers it is holding. **Rig:** 1, 2 or 3.

Ask something long enough to still be streaming — a summary of the whole filing,
not "hello" — then, while it streams:

```bash
kill -TERM <uvicorn pid>                              # Rig 1 / 2
kubectl -n cfa rollout restart deploy/cfa-backend     # Rig 3
```

Watch the leaving instance:

```
INFO  api.socket  Waiting up to 120s for 1 run(s) to finish
INFO  api.socket  All 1 in-flight run(s) finished
```

Pass: both lines, and the answer is in the ledger when you reopen the dossier.
Fail: the process exits immediately, and the dossier holds a question with no
answer.

**One ordering detail that looks like a failure and is not:** `done` reaches the
browser *before* `_record_answer` writes the row, so a reload in that instant
shows a question with no answer even on a healthy deployment. Wait a second
before calling it a bug.

### 7b. The grace period (Rig 3)

The drain only helps if the kubelet lets it finish. `preStop` sleep +
`SHUTDOWN_DRAIN_SECONDS` must fit inside `terminationGracePeriodSeconds` — one
budget, three numbers.

```bash
kubectl -n cfa get pod <terminating-pod> -w
```

Pass: the pod exits *before* the grace period (180s). A pod that takes exactly
180 seconds every time is being SIGKILLed, and the answers it was holding are
gone. The negative is one patch:

```bash
kubectl -n cfa patch deploy/cfa-backend \
  -p '{"spec":{"template":{"spec":{"terminationGracePeriodSeconds":5}}}}'
```

Repeat 7a: the "Waiting up to 120s…" line appears with no "finished" line after
it, and the question sits unanswered. Restore with `kubectl apply -k
deploy/minikube`.

Worth doing once as a separate experiment: `SHUTDOWN_DRAIN_SECONDS=0` with the
grace period left alone. The answer is lost even though the pod had three
minutes — which is how you learn that the drain, not the grace period, is what
saves it.

### 7c. The sweep

**Proves:** a run that died without warning gets closed out instead of leaving a
question waiting forever.

Strand a run — a death with no SIGTERM at all:

```bash
kubectl -n cfa delete pod <pod> --grace-period=0 --force     # mid-answer
```

Confirm the strand with the "questions with nothing after them" query in
[§15](#15-sql-to-keep-beside-you). Then make it old enough to sweep, without
waiting half an hour — either back-date the row on a scratch database:

```sql
UPDATE messages SET created_at = created_at - interval '2 hours' WHERE id = '…';
```

or shorten the cutoff for one run:

```bash
kubectl -n cfa set env deploy/cfa-backend STALE_RUN_MINUTES=1
```

Restart a pod. The sweep runs at startup, under the housekeeping lock:

```
INFO  conversations.service  Closed out 1 interrupted run(s) from a previous life
```

and the dossier now ends with *"This run was interrupted before it finished…"*
(`status = 'error'`) instead of silence.

### 7d. The mistake the cutoff exists to prevent

Worth staging once, because it is the failure mode that a "more aggressive
sweep" would create. Leave `STALE_RUN_MINUTES=1`, start a long question on pod
A, and restart pod B while it is still streaming. B's sweep finds a
minute-old question with nothing after it, cannot tell it from a dead one, and
answers it "interrupted" — a live run, marked dead, while the analyst watches
the real answer arrive.

Put the cutoff back to 30 and run exactly the same test: the run is left alone.
That is the entire argument for a generous default, in one experiment.

```bash
kubectl -n cfa set env deploy/cfa-backend STALE_RUN_MINUTES-
```

---

## 8. Break #5 — two writers, one position

**Proves:** concurrent writes into one dossier all land — positions 1…N, no
gaps, nothing silently dropped. **Rig:** any; this one needs no second instance.

The break with no visible symptom. The loser's `IntegrityError` is caught,
because bookkeeping should not fail a request, so there is no 500, no toast and
nothing in the UI — just an answer the analyst watched arrive and cannot find
later. **The only evidence is in the ledger**, so judging this test from the
browser is judging it from the one place the bug is invisible.

### By hand

Open one dossier in two browser tabs, same account, type a question in each, and
send them within a second of each other. Then:

```sql
SELECT seq, role, status, left(content, 50)
FROM messages WHERE conversation_id = '…' ORDER BY seq;
```

Pass: four rows — two questions, two answers — seq 1…4, no gaps, whatever the
interleaving. Fail: three rows, or a question whose answer never appears.

Two instances make it ordinary rather than rare: same test, but with one tab
pointed at :8001 and the other at :8002.

### The everyday version

Upload a filing while a question is in flight in the same dossier. Both paths
write the conversation row; the lock is what serialises them. Pass: the filing
is in the dock *and* the answer is in the ledger.

### After any concurrency test

Run the gap query and the `message_count` query from
[§15](#15-sql-to-keep-beside-you). `message_count` disagreeing with the row
count is the same bug seen from the conversation row.

### The automated version, and how to watch it fail

`coordination.py` already runs twelve concurrent writers into one conversation —
run it after any change to `conversations/service.py`:

```bash
backend/Analyzer/.venv/bin/python deploy/checks/coordination.py \
    --database-url postgresql+asyncpg://analyzer:analyzer@127.0.0.1:5432/filing_analyzer \
    --redis-url ""
```

To see what it is guarding, comment out the `with_for_update()` statement at the
top of `record_message` **on a scratch checkout** and run it again:

```
without the row lock:  2/12 writes landed, 10 IntegrityErrors
with the row lock:    12/12 writes landed, seq 1…12
```

Ten lost answers, no errors anywhere a user could see. That is the shape of the
bug in production.

---

## 9. Break #6 — broadcasting, before the feature exists

**Proves:** nothing today, and that is the finding. Every event is emitted to
the connection that asked for it, so there is no cross-pod delivery to get
wrong. The check is that it stays true:

```bash
grep -rn "sio.emit" backend/Analyzer | grep -v "to=sid"
```

No hits. A hit means somebody added a broadcast, and the same day, the test to
write is:

1. Two instances, `SOCKETIO_MESSAGE_QUEUE_URL` unset.
2. Connect a client to A; trigger the broadcast on B.
3. The client on A receives **nothing** — that is the break, and it is silent.
4. Set `SOCKETIO_MESSAGE_QUEUE_URL=redis://…` on both, restart, repeat: it
   arrives.

Write it the day of the first `enter_room`, not after the bug reports.
Unhandled, this one fails for `(N-1)/N` of recipients and never raises.

---

## 10. Postgres connections

**Proves:** N instances × their pools fit inside `max_connections`, and nothing
holds a pooled connection across a model call.

Each instance opens `DB_POOL_SIZE` (5) plus `DB_MAX_OVERFLOW` (10). The
arithmetic is the whole test: 8 instances is 120 against a default 100.

```sql
SELECT count(*) FROM pg_stat_activity WHERE datname = 'filing_analyzer';
SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'filing_analyzer' GROUP BY state;
SHOW max_connections;
```

Drive a few concurrent questions and an upload while you watch. Pass: the count
climbs toward the pool size and comes back down. Fail: it sits at the ceiling,
or climbs past what the pools should allow.

**The negative, cheaply.** Rather than restarting Postgres with a small
`max_connections`, make the pools obviously too big for a run:

```bash
kubectl -n cfa set env deploy/cfa-backend DB_POOL_SIZE=60 DB_MAX_OVERFLOW=60
```

Two instances now want 240. Under load you get the message you would otherwise
meet at 3am — `FATAL: sorry, too many clients already` — and the app fails at
the point of asking a question rather than at startup, which is worth seeing
once. Undo with `DB_POOL_SIZE- DB_MAX_OVERFLOW-`.

**The regression to keep watching:** the summariser used to hold a session
across the model call. Trigger folds ([§11](#11-rolling-summaries-and-the-lease))
and watch for `state = 'idle in transaction'` — near-zero is correct. One per
fold, held for tens of seconds, means somebody put a model call back inside a
session, and a handful of concurrent folds will then starve the pool.

---

## 11. Rolling summaries and the lease

**Proves:** two instances do not fold the same dossier twice, and that a
duplicate fold would cost a model call rather than correctness.

Make folds happen quickly instead of after 24 messages:

```bash
kubectl -n cfa set env deploy/cfa-backend HISTORY_SUMMARY_THRESHOLD=4
```

Ask five or six short questions in one dossier, alternating instances if you
have two. Every instance should have said this at startup:

```
INFO  core.leases  Cross-instance leases ready
```

and exactly one of them should log each fold:

```
INFO  conversations.service  Summarised <id> through seq 6 (4 message(s) folded)
```

**The negative:** stop Redis and restart both instances, so leases are off
(`REDIS_URL not set` or `Redis unreachable (…) — running without leases`). Now
both instances may fold the same dossier — and the point of the test is that
this is *not* a correctness failure. Prove it:

```sql
SELECT id, message_count, summary_through_seq, summary_tokens
FROM conversations WHERE id = '…';
```

`summary_through_seq` must only ever move forwards. A slow fold that lands after
a newer one is discarded rather than applied (`Discarding a stale fold …`, at
DEBUG). Two folds cost one wasted call to the model that is already the
bottleneck — which is why the lease is best-effort in Redis and not a lock in
Postgres.

Put the threshold back (`HISTORY_SUMMARY_THRESHOLD-`) when you are done, or
every dossier you touch afterwards will fold constantly.

---

## 12. Startup housekeeping, and the schema race

**Proves:** work that must happen once happens once, and two instances starting
together do not race each other to create the schema.

### The housekeeping lock

```bash
kubectl -n cfa rollout restart deploy/cfa-backend
kubectl -n cfa logs -l app.kubernetes.io/name=cfa-backend --tail=-1 --prefix \
  | grep -E 'housekeeping|Closed out|orphan'
```

Pass: one pod does the work, the other says

```
INFO  main  Another instance is doing the startup housekeeping
```

and neither of them waited. Nothing here is fatal if it goes wrong — two sweeps
at once are caught by the row-lock re-check — but the line is your evidence the
lock is being taken at all.

### The schema race

The one that looks like a database problem and is a startup race. **Scratch
database only — this destroys the accounts and the ledger.**

```sql
DROP SCHEMA public CASCADE; CREATE SCHEMA public;
```

Then start both instances at the same moment:

```bash
kubectl -n cfa scale deploy/cfa-backend --replicas=0
kubectl -n cfa scale deploy/cfa-backend --replicas=2
```

Pass: both pods reach Ready, and the tables exist once. Unhandled, this is two
`CREATE TABLE`s and a `DuplicateTable` on the loser — an intermittent
CrashLoopBackOff that appears only during rolling deploys, which is exactly when
nobody wants to be debugging it. The lock here is blocking and held to commit,
so the second pod waits and then reflects a finished schema.

Afterwards, expect the startup prune to clear the vector collections the dropped
schema left orphaned — a useful sighting of that path doing its job.

---

## 13. Redis — proving it fails soft

**Proves:** a Redis outage costs one extra `SELECT` per question, not an outage.

```bash
kubectl -n cfa scale deploy/redis --replicas=0
```

Then run the whole analyst path: sign in, upload, ask, reload, read the history
back. Pass: all of it works, and both subsystems say what happened:

```
WARNING  conversations.cache  Message cache disabled after a Redis error: …
WARNING  core.leases          Leases disabled after a Redis error: …
```

**Both stay off until the instance restarts**, deliberately — reconnecting per
request would mean paying a timeout per request for as long as Redis is down. So
when you bring Redis back, restart the backends too, or you will conclude the
cache is broken when it is merely switched off:

```bash
kubectl -n cfa scale deploy/redis --replicas=1
kubectl -n cfa rollout restart deploy/cfa-backend
```

Also worth one cold start with `REDIS_URL=""` — the app should say
`REDIS_URL not set — running without a message cache` and behave identically,
slower. A deployment that *needs* Redis to be correct has a bug in it.

---

## 14. Ordering, and the week-old pod

### Cache ordering across instances

Cached tails are appended by whichever instance recorded the message, so two
instances writing one dossier can push rows in a different order to the
positions they were given. The sort on the way out is what makes that
harmless — check it by reading the same ledger from both instances:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/api/conversations/$SID/messages
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18002/api/conversations/$SID/messages
```

Pass: identical responses, in `seq` order. Then flush the cache and read again —
`redis-cli FLUSHALL`, or `kubectl -n cfa exec deploy/redis -- redis-cli FLUSHALL`
— and the answer must be the same, only slower. Cache and database agreeing is
the property; a difference between them means the cache is authoritative
somewhere it should not be.

### The week-old pod

`VectorService` keeps an LRU of open Chroma handles, capped at 512. The failure
it replaced only appears on a pod that has been up long enough to have answered
in more dossiers than that — which is precisely the pod a stable deployment has,
and never the pod you test on.

There is no five-minute version of this test. The honest one is an observation:
compare `kubectl top pod` for a pod that has been up a week against a fresh one,
and expect the difference to be flat. If you want to force it, ask questions
across more than 512 dossiers on one instance and watch RSS level off rather
than climb.

---

## 15. SQL to keep beside you

Get a prompt:

```bash
psql postgresql://analyzer:analyzer@localhost:5432/filing_analyzer          # Rig 1 / 2
kubectl -n cfa exec -it postgres-0 -- psql -U analyzer -d filing_analyzer   # Rig 3
```

```sql
-- One dossier's ledger, in order. The first thing to look at after any
-- concurrency test.
SELECT seq, role, status, left(content, 60) AS content
FROM messages WHERE conversation_id = '…' ORDER BY seq;

-- Did anything get lost? Rows and the high-water mark must agree.
SELECT count(*) AS row_count, max(seq) AS high_water, count(*) = max(seq) AS intact
FROM messages WHERE conversation_id = '…';

-- Two writers claiming one position. The constraint makes this impossible;
-- run it anyway after touching record_message.
SELECT conversation_id, seq, count(*)
FROM messages GROUP BY conversation_id, seq HAVING count(*) > 1;

-- Questions with nothing after them: runs that never came back. Recent ones
-- are in flight; old ones are what the sweep is for.
SELECT m.conversation_id, m.seq, m.created_at, left(m.content, 60)
FROM messages m
WHERE m.role = 'user'
  AND NOT EXISTS (SELECT 1 FROM messages later
                  WHERE later.conversation_id = m.conversation_id
                    AND later.seq > m.seq)
ORDER BY m.created_at DESC;

-- The conversation row's own bookkeeping, against the rows themselves.
SELECT c.id, c.message_count, count(m.id) AS actual
FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
GROUP BY c.id, c.message_count HAVING c.message_count <> count(m.id);

-- What the sweep closed out.
SELECT conversation_id, seq, created_at
FROM messages WHERE status = 'error' ORDER BY created_at DESC LIMIT 20;

-- Summaries move forwards only.
SELECT id, message_count, summary_through_seq, summary_tokens
FROM conversations WHERE id = '…';

-- Connections, against the ceiling.
SELECT state, count(*) FROM pg_stat_activity
WHERE datname = 'filing_analyzer' GROUP BY state;
SHOW max_connections;
```

---

## 16. Load, and what it will not tell you

Load testing this app mostly measures Ollama. Know that before you draw a
conclusion from it.

**What adding instances does not fix.** Answer latency. Every instance queues
against the same model host, so two instances answering four concurrent
questions is the same queue as one instance answering four — with more
connections, more pools and more memory spent to arrive there.

**What is worth measuring, and what to expect:**

| Measure | With 1 instance | With 2 | What it tells you |
| --- | --- | --- | --- |
| Time to first token, one question at a time | baseline | the same | the model, not the app |
| Time to first token, 4 concurrent | baseline | the same or slightly worse | the queue is at Ollama |
| Uploads finished per minute | baseline | roughly double | embedding is CPU work *in* the API instance — this one really does scale |
| Postgres connections | pool | 2 × pool | [§10](#10-postgres-connections) |
| Behaviour during a rollout | an outage | a reconnect | the actual reason to run two |

**How to generate load honestly.** Several browser tabs is a fine start.
Anything heavier should be pointed at the model host directly first, so you know
what its ceiling is before you attribute that ceiling to the API.

The conclusion this usually reaches is the one in
[SCALING.md §14](SCALING.md#14-should-you-scale-out-at-all): scale out for
availability and upload throughput, not for answer throughput.

---

## 17. False passes

Tests that pass without having tested anything. Every one of these has happened.

| The test | Why it passed | What to do instead |
| --- | --- | --- |
| Split test through the ingress | the sticky cookie sent upload and question to the same pod | address pods directly, one `port-forward` each |
| Break #2 negative on Rig 2 | both processes' embedded stores are literally the same directory | second tree, or Rig 3 |
| Token portability on one instance | there was nothing to be portable across | two instances, addressed separately |
| Stickiness tested over WebSocket | one connection cannot split | force polling |
| Drain tested with a short question | it finished before the SIGTERM mattered | ask something that is still streaming |
| Sweep tested immediately after stranding | the cutoff is 30 minutes | back-date the row, or shorten `STALE_RUN_MINUTES` |
| Concurrency judged from the browser | the loser's error is swallowed; the UI shows an answer that was never stored | read the ledger in SQL |
| Logs read with the default tail | the startup lines had already scrolled away | `--tail=-1`, `--prefix`, every pod |
| One pod's logs grepped, not all | the misconfigured pod was the other one | `-l app.kubernetes.io/name=cfa-backend` |
| A dossier reused between tests | the answer came from history, not from the store under test | one fresh dossier per test |
| Redis "still broken" after coming back | the client disables itself until restart, by design | restart the instances too |
| `smoke.py` passing | it passes on a single replica too | `split.py`, or the manual split in [§5](#5-break-2--the-shared-vector-store) |

---

## 18. The evidence table

One line per test — the thing you actually look at.

| Test | Pass looks like |
| --- | --- |
| Shared signing key | `grep -c "JWT_SECRET_KEY is not set"` → 0, on every instance |
| Token portability | `/api/auth/me` returns 200 on an instance that did not mint the token |
| Shared store, configured | `VectorService ready (… shared=True …)` on every instance |
| Shared store, working | upload on A, ask on B, answer cites the filing; B logs `Retrieved N chunk(s)` |
| Store gate | pods stay in `Init:1/2` with `waiting for chroma at …` |
| Sticky cookie | `set-cookie: cfa-sticky=…` on the handshake response |
| Sticky routing | ten cookie-carrying handshakes, one pod in the logs |
| Polling in a browser | one `sid`, no connect/disconnect loop |
| Drain | `Waiting up to 120s for 1 run(s) to finish` **and** `All 1 in-flight run(s) finished` |
| Grace period | the pod exits before 180s, not at it |
| Sweep | `Closed out N interrupted run(s) from a previous life`, and the "interrupted" row in the dossier |
| Live run not swept | a streaming question is still unanswered after another instance restarts |
| Housekeeping lock | exactly one `Another instance is doing the startup housekeeping` |
| Schema race | both instances Ready after a `DROP SCHEMA`, no `DuplicateTable` |
| Message positions | `count(*) = max(seq)`, every question followed by an answer |
| Pool sizing | `pg_stat_activity` comfortably under `max_connections`, no `idle in transaction` during folds |
| Summary lease | one `Summarised … through seq N` per crossing; `summary_through_seq` never decreases |
| Redis fail-soft | the whole path works with Redis at zero replicas, two `disabled after a Redis error` warnings |
| Cache ordering | both instances return the same messages in `seq` order, cached or not |
| No broadcasts | `grep -rn "sio.emit" backend/Analyzer \| grep -v "to=sid"` → nothing |

---

## 19. A session, in order

Rig 3, about forty-five minutes, arranged so each restart earns its keep.

1. **Read the startup lines first.** `--tail=-1 --prefix`, both pods: no
   `JWT_SECRET_KEY is not set`, two `shared=True`, one housekeeping winner and
   one that skipped. Three of the checks, before you have touched anything.
2. **`smoke.py`** — if the analyst path is broken, nothing below means anything.
3. **The manual split** ([§5](#5-break-2--the-shared-vector-store)), then
   **`split.py`** to confirm you would have caught it automatically.
4. **The sticky cookie and the ten handshakes** ([§6](#6-break-3--the-sticky-handshake)).
5. **Two tabs into one dossier**, then the ledger queries
   ([§8](#8-break-5--two-writers-one-position)).
6. **A long question and a rollout** — drain lines, exit before the grace
   period, the answer in the ledger ([§7](#7-break-4--the-drain-and-the-sweep)).
7. **Force-kill mid-answer, back-date the row, restart** — the sweep closes it
   out.
8. **Take Redis away**, run `smoke.py` again, put it back *with* a restart.
9. **Break something on purpose** — `CHROMA_HOST=""` is the best value for
   money — and watch the split fail on exactly one step. Undo it.
10. **`coordination.py`**, last, for the locks and leases underneath.

**If you only have ten minutes:** the startup lines (1), the manual split (3),
and the drain (6). Those three cover the two blockers and the one break that
costs an answer on every deploy.

---

## See also

- [SCALING.md](SCALING.md) — what each break is, and where it is handled
- [DEPLOYMENT.md](DEPLOYMENT.md) — the rigs themselves, and breaking them on purpose
- [`deploy/checks/README.md`](../deploy/checks/README.md) — the three scripts
- [DB-OPERATIONS.md](DB-OPERATIONS.md) — every read and write, when a query surprises you
- [SOCKETIO.md](SOCKETIO.md) — the real-time layer, for the handshake tests
