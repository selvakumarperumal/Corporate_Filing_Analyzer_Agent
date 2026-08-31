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

One block per row of the matrix above, same numbers. Every block is laid out
the same way:

- **Complaint** — what a user would actually say when this breaks.
- **In one sentence** — the whole problem, no jargon.
- **How it works** — the mechanism, in small steps and small tables.
- **Settings involved** — the environment variables, what each one does, and
  what happens if it is wrong.
- **Example** — a walk-through with real numbers, log lines or SQL.

If you read nothing else in a block, read *In one sentence*.

---

#### 1 · JWT signing key

**Complaint:** *"It logs me out at random. I sign in again, it works, then it
happens again."*

**In one sentence.** Each server invents its own signature at startup, so the
login ticket one server hands out is unreadable to all the others.

**How it works.** Signing in gives the browser a ticket with a signature on it.
Only something holding the signing key can make that signature, or check it.
With `JWT_SECRET_KEY` unset, each server makes up its own key while it boots —
a different one per server, and a new one on every restart.

So Pod A's signature means nothing to Pod B. The browser's backup plan for a
rejected ticket — spend the refresh ticket, get a fresh one — fails the same
way, because that one carries the same unrecognised signature. Two rejections
in a row is what the browser treats as "signed out".

```mermaid
sequenceDiagram
  autonumber
  participant U as Browser
  participant A as Pod A, key a1b2
  participant B as Pod B, key z9y8
  U->>A: sign in
  A-->>U: ticket signed a1b2
  U->>B: next request, same ticket
  B-->>U: 401, that signature is not mine
  U->>B: backup plan, the refresh ticket
  B-->>U: 401, also signed a1b2
  Note over U,B: two rejections = back to the login page
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `JWT_SECRET_KEY` | The signature on every ticket. Must be identical on every instance. | *(empty — a random key per process)* | logins fail at random across pods, and every restart signs everyone out |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | How long a ticket is good for | `15` | too short = constant refreshes; too long = a stolen ticket lives longer |
| `REFRESH_TOKEN_EXPIRE_DAYS` | How long the backup ticket is good for | `14` | shorter means people log in again more often |

**Example.** Four pods, no shared key. Each request has a one-in-four chance of
landing back on the pod that issued the ticket, so **three of every four fail**.
Never twice in a row, never reproducible on demand — which is exactly why it
gets filed as "flaky login" rather than as an outage.

On two local instances with the key unset:

```
A 200      <- the pod that signed the ticket
B 401      <- any other pod
```

And before anyone has even logged in:

```
WARNING  main  Tokens are signed with a key that dies with this process…
```

**Fix:** the same `JWT_SECRET_KEY` on every instance, from one Secret.
**Yours.** → [§4](#4-break-1--the-shared-signing-key)

---

#### 2 · Embedded vector store

**Complaint:** *"It answered my question, but it clearly has not read the filing
I just uploaded."*

**In one sentence.** The uploaded filing is saved on one server's own disk, so
any question answered by a different server is answered without it.

**How it works.** Uploading a filing means splitting it into chunks, turning
each chunk into a vector, and saving those vectors in Chroma. "Embedded" Chroma
is just a folder on the disk of the server that did the work. No other server
can see it.

Asking a question means searching those vectors and handing the best chunks to
the model. Land on the wrong server and the search finds nothing.

Here is the cruel part: **finding nothing is a legitimate outcome.** A question
about something the filing does not cover also finds nothing. So there is no
error to raise — the model answers from general knowledge, in exactly the same
confident voice, and the screen looks normal.

```mermaid
flowchart TB
  U["09:00 upload apple-10k.pdf"] --> A["Pod A splits and saves it"]
  A --> DA[("Pod A disk: 412 chunks")]
  Q["09:01 what were the research costs?"] --> LB{"balancer picks a pod"}
  LB -->|"Pod A"| GOOD["searches 412 chunks<br/>answer quotes the filing"]
  LB -->|"Pod B"| DB[("Pod B disk: 0 chunks")]
  DB --> BAD["Retrieved 0 chunk(s)<br/>fluent answer, no filing behind it"]
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `CHROMA_HOST` | Address of a shared Chroma server. Blank means "use my own disk". | *(empty — embedded)* | every pod has a private set of filings; uploads are invisible to the other pods |
| `CHROMA_PORT` | Port of that server | `8000` | the pod cannot reach the store and the init container holds it back |
| `CHROMA_SSL` | Talk to it over HTTPS | `false` | connection refused against a TLS-only store |
| `CHROMA_COLLECTION` | Name of the collection filings go into | `corporate_filings` | two deployments sharing one Chroma read each other's filings, or neither finds theirs |

Do not "fix" this by pointing two pods at one shared volume — an embedded store
is a library writing SQLite on local disk, and two writers corrupt the index.
The only fix is a Chroma server they both talk to over HTTP.

**Example.** Two pods, so roughly **half** of all questions about a freshly
uploaded filing are answered from nothing. Four pods and it is three quarters.

Upload through A, ask through B, read B's log:

```
INFO  …vector_store  Retrieved 0 chunk(s) for …
```

The answer will contain no figure that appears in the document. That is the
tell, and it is the only one — which is why this is the most important test in
the file. Everything else fails loudly; this one fails by looking like success.

**Fix:** one Chroma server every instance points at, plus the init container so
a pod that cannot reach it never serves traffic. **Yours.**
→ [§5](#5-break-2--the-shared-vector-store)

---

#### 3 · Handshake routing

**Complaint:** *"The page connects, drops, connects again. Forever."*

**In one sentence.** Opening a live connection takes several requests that must
all reach the *same* server, and a normal load balancer spreads them around.

**How it works.** A WebSocket is not always available, so Socket.IO opens with
ordinary HTTP polling: one request to start a session, then more requests that
refer back to it by its id (`sid`).

That `sid` lives in the memory of the server that answered the first request.
It is not in Postgres and not in Redis — there is nowhere else to look it up.

A round-robin balancer sends request two somewhere else. That server has never
heard of the sid and says so. The browser does the correct thing — starts a
fresh handshake — and gets split again. That is the loop.

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
  LB->>B: (round robin, other pod)
  B-->>C: never heard of sid-1
  Note over C,B: browser restarts, same coin toss
```

With a sticky cookie the first reply names the pod, the balancer reads that
cookie on every later request, and all of them land on Pod A.

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| *(none in the app)* | Stickiness is an **ingress** setting, not an application one. Nothing you can change in the backend affects it. | — | the routing is already decided before the request reaches your code |
| `CORS_ORIGINS` | Which origins the browser may call the API from | `["*"]` | the handshake is blocked by the browser before routing is even involved |

**Example.** Two pods means about **half** of all handshakes fail, so the app
"works if you refresh a couple of times". In the browser's network tab you can
watch it: a new `sid` every second or two, none of them lasting.

The one fact to check first:

```
set-cookie: cfa-sticky=…      <- on the handshake response
```

No cookie, no stickiness. And do not test this with a WebSocket — a WebSocket
is one long connection that cannot be split, so it passes even when polling is
completely broken.

**Fix:** cookie affinity on the ingress. **Yours.**
→ [§6](#6-break-3--the-sticky-handshake)

---

#### 4 · In-flight runs — the drain

**Complaint:** *"I asked a question, the answer started appearing, then it
stopped. It is still spinning."*

**In one sentence.** Writing an answer takes about a minute, and if the server
is shut down during that minute the answer is never saved — the question is left
hanging forever.

##### First: what is SIGTERM?

A program never stops by itself. Something has to **tell** it to stop, and on
Linux that message is called a **signal**. Two of them matter here:

| Signal | What it means | Does the program get to run any code? | Who sends it |
| --- | --- | --- | --- |
| **`SIGTERM`** | "please stop when you can" | **Yes.** It can finish what it is doing, save, close files, then exit on its own | `kill <pid>`, `docker stop`, a Kubernetes rollout, scaling down, draining a node |
| **`SIGKILL`** | "stop now" | **No.** The kernel removes the process. Nothing runs — no saving, no last write | `kill -9`, the out-of-memory killer, the grace-period timeout, a node dying |

Think of `SIGTERM` as being told *"wrap up and go home"* — you get to save your
file first. `SIGKILL` is the power being cut.

**See it for yourself in thirty seconds.** Start a program that says something
when it is asked to stop:

```bash
python3 -c '
import signal, time
signal.signal(signal.SIGTERM, lambda *_: (print("SIGTERM — saving my work"), exit(0)))
print("working…"); time.sleep(300)' &
kill %1          # SIGTERM
```

```
working…
SIGTERM — saving my work
```

Now run exactly the same thing and use `kill -9 %1` instead:

```
working…
[1]+  Killed    python3 -c …
```

No *"saving my work"* line — the handler never got to run. **That is the whole
difference**, and the rest of this row is about which of the two your deploys
are doing.

Every ordinary deploy sends `SIGTERM` first: a new image, `rollout restart`,
scaling from 4 pods to 2, a node being drained. It is the normal, polite path —
and this app's shutdown code only runs on that path.

##### Second: what is a "run", and what does "in flight" mean?

- **A run** is the job that answers *one* question. It starts when the analyst
  presses send and ends when the last word has been saved.
- It lives **only inside that one server process**, in memory. No other server
  knows it exists. There is no queue, no job table, nothing on disk.
- **In flight** means started and not finished yet.

The word is borrowed from aircraft, and the analogy is exact: when an airport
closes, planes already in the air still have to land. You cannot un-take-off a
run. You either let it finish, or it is lost.

##### What gets saved, and when

One question is **two** writes to the database, far apart:

| Time | What is happening | Saved? |
| --- | --- | --- |
| 0s | analyst presses send | **yes** — the question row |
| 0–60s | the model writes the answer, word by word, streaming to the browser | no, not yet |
| 60s | the last word arrives | **yes** — the answer row |

For that whole minute the database holds a question with no answer. **That is
normal** — while the run is alive.

##### A concrete example, second by second

Priya asks *"summarise the risk factors"* at **14:30:00**. Someone starts a
deploy at **14:30:20**, twenty seconds later.

**Without the drain:**

| Clock | What Priya sees | What the server does | In the database |
| --- | --- | --- | --- |
| 14:30:00 | her question appears | writes the question as message 7, starts the run | Q7 |
| 14:30:05 | words start appearing | the model is generating | Q7 |
| 14:30:20 | words still appearing | **`SIGTERM` arrives** — the deploy | Q7 |
| 14:30:20 | **text freezes mid-sentence** | the process exits instantly; the run dies with it | Q7 |
| 14:35:00 | still spinning | (this server no longer exists) | Q7 |
| next week | still spinning | — | **Q7, alone, forever** |

**With the drain:**

| Clock | What Priya sees | What the server does | In the database |
| --- | --- | --- | --- |
| 14:30:20 | nothing changes | `SIGTERM` — stops accepting **new** work, waits for the 1 run it is holding | Q7 |
| 14:30:35 | words still appearing | still waiting | Q7 |
| 14:31:00 | the answer finishes normally | writes the answer as message 8 — **then** exits | Q7 + A8 |

The drain is the airport rule: **no new take-offs, but the planes already in the
air get to land.**

##### "Why doesn't it just retry?"

This is the question everyone asks, and the answer is what makes the row matter:

- Nothing wrote down "this question still needs an answer". The only record that
  a run existed was the run itself, in that process's memory.
- The browser is not retrying either. It is holding a socket that just closed,
  waiting for words that will never come.
- The next server to start has no way to tell a question that is *stranded* from
  a question that is *being answered right now on another server*. (That is the
  sweep, in the next row, and it is why it waits 30 minutes.)

```mermaid
flowchart TB
  T["SIGTERM arrives<br/>20s into a 60s answer"] --> N["no drain: exit immediately"]
  T --> D["drain: no new work,<br/>wait for the 1 run in flight"]
  N --> N2["question saved, answer never saved<br/>Priya waits forever"]
  D --> D2["answer saved at 60s<br/>then the server exits"]
```

##### Settings involved

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `SHUTDOWN_DRAIN_SECONDS` | How many seconds shutdown waits for answers still being written | `120` | too low and slow answers are cut off. It must be **longer** than your slowest answer and **shorter** than the platform's grace period |
| `terminationGracePeriodSeconds` | Not an env var — a Kubernetes manifest field. How long the platform waits after `SIGTERM` before sending `SIGKILL`. | `30` | set below the drain and the drain never gets to finish — that is the Grace period row |

##### Example — prove it on your own deployment

Ask for something long (a summary of a whole filing, not "hello"), and while the
words are still streaming:

```bash
kill -TERM <uvicorn pid>                            # locally
kubectl -n cfa rollout restart deploy/cfa-backend   # on Kubernetes
```

A pass is **both** of these lines in the leaving server's log:

```
INFO  api.socket  Waiting up to 120s for 1 run(s) to finish
INFO  api.socket  All 1 in-flight run(s) finished
```

A fail says exactly what it cost:

```
WARNING  api.socket  1 run(s) did not finish in time — their answers are lost…
```

Then check the database, because the log is a claim and the ledger is the
evidence. Every `user` row must be followed by an `assistant` row:

```sql
SELECT seq, role, status FROM messages
WHERE conversation_id = '…' ORDER BY seq;
```

And the negative worth doing once: `kill -9` instead of `kill -TERM`. No drain
lines appear at all, because no code ran — which is the next row.

**Why more servers make it worse.** A rolling deploy stops every pod in turn, so
every answer being written *anywhere* in the deployment is at stake on every
deploy. With one instance you only paid this at restarts.

**Fix:** wait for in-flight runs on shutdown. **Handled** — the test is that the
wait really happens. → [§7](#7-break-4--the-drain-and-the-sweep)

---

#### 4 · In-flight runs — the sweep

**Complaint:** *"That question from Tuesday is still thinking."*

**In one sentence.** Some deaths give the server no chance to run any code at
all, so a cleanup job at startup finds the questions those deaths abandoned and
writes "this was interrupted" — otherwise they spin forever.

##### Why the drain is not enough

The drain from the row above only runs when the server is asked politely —
`SIGTERM`. These deaths ask nothing:

| How the server dies | Signal | Does the drain run? | Who cleans up |
| --- | --- | --- | --- |
| deploy, restart, scale-down | `SIGTERM` | **yes** | nobody needs to |
| out of memory | `SIGKILL` | no | the sweep |
| grace period expired mid-drain | `SIGKILL` | no — it was cut off | the sweep |
| the node disappears | *nothing at all* | no | the sweep |

The wreckage is identical in every case: **a question row with nothing after
it**, and nobody coming back for it.

##### A concrete example

Ravi asks a question at **09:15:00**.

| Clock | What happens | In the database |
| --- | --- | --- |
| 09:15:00 | question saved as message 7, the run starts | Q7 |
| 09:15:10 | the pod uses too much memory; Linux sends `SIGKILL` | Q7 |
| 09:15:10 | the process is gone — **no code ran**, nothing was saved | Q7 |
| 09:15:11 | Kubernetes starts a replacement pod | Q7 |
| 09:15:11 | the new pod sweeps, but Q7 is **11 seconds old** — too new to touch | Q7 |
| 09:45:00 | Ravi's browser has long since given up. The question still shows as unanswered | Q7 |
| the next startup after 09:45 | Q7 is now older than 30 minutes → the sweep writes the interrupted answer | Q7 + A8 *(error)* |

Two things in that table surprise people, and both are deliberate:

1. **The sweep is not immediate.** It runs *at startup only*, and only touches
   questions older than `STALE_RUN_MINUTES` (30). So the pod that replaces the
   dead one usually heals nothing — a later start does.
2. **That delay is the point.** Read the next section for why.

##### The hard part — why it waits 30 minutes

A question being answered **right now, on another server** looks *exactly* the
same as an abandoned one: a question row, no answer row yet. There is no flag
distinguishing them, because the run only exists in another process's memory.

Mark that one and you have faked a failure on a perfectly healthy run — while
the analyst is watching the words arrive. That is worse than the problem being
fixed.

| Question written | Answer row | Age | What the sweep does |
| --- | --- | --- | --- |
| 10:00 | none | 2 minutes | **nothing** — another server may be answering it right now |
| 10:00 | none | 45 minutes | writes the "interrupted" row |
| 10:00 | 10:01 | any | nothing — it finished |

No real run lives for 30 minutes, so anything older than that is safe to declare
dead. Each candidate is also re-checked under the conversation's lock
immediately before writing, in case an answer landed in the meantime.

##### What the analyst ends up seeing

Instead of a spinner that never resolves, the dossier shows an answer marked as
an error:

> *"This run was interrupted before it finished — the server restarted while the
> answer was being written. Ask again to get an answer."*

That is the entire value of this row: **a spinner tells you nothing and can be
waited on forever; an error tells you to ask again.**

```mermaid
flowchart TB
  K["server dies with no warning<br/>SIGKILL, out of memory, lost node"] --> L["question saved, answer never saved"]
  L --> S{"at a later startup:<br/>is it older than 30 minutes?"}
  S -->|"no"| SKIP["leave it — it may be live on another server"]
  S -->|"yes"| W["write the interrupted answer, marked as an error"]
  W --> R["the analyst can ask again"]
```

##### Settings involved

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `STALE_RUN_MINUTES` | How old an unanswered question must be before the sweep is allowed to touch it | `30` | too low and the sweep marks **live** runs on other servers as failed — worse than the problem. Too high and analysts stare at spinners for longer |
| `SHUTDOWN_DRAIN_SECONDS` | The polite path from the row above | `120` | a working drain means the sweep rarely finds anything, which is the goal |

##### Example — strand a run on purpose

Kill the process the rude way, mid-answer, so nothing drains:

```bash
kill -9 <uvicorn pid>
```

The question is now stranded, but it is seconds old, so a restart will ignore
it. Rather than waiting half an hour, **move it into the past**:

```sql
UPDATE messages SET created_at = now() - interval '45 minutes'
WHERE conversation_id = '…' AND seq = 7;
```

Now restart, and the startup log says:

```
INFO  conversations.service  Closed out 1 interrupted run(s) from a previous life
```

Run the negative too, because it is the half that can hurt you: while instance B
is streaming a long answer, restart instance A. B's live run must **not** be
swept — the question should still be unanswered when A comes back, and B should
finish it normally a moment later.

**Fix:** close out abandoned runs at startup and leave live ones alone.
**Handled** — that cutoff is the whole difficulty. → [§7c](#7c-the-sweep)

---

#### 4 · Grace period

**Complaint:** *"The drain is right there in the logs, and the answer is still
gone."*

**In one sentence.** Your server asks for two minutes to finish its answers;
Kubernetes gives it thirty seconds and then kills it — so the drain never gets
to finish.

**Two clocks start at the same moment, and they do not talk to each other**

| Clock | Owned by | Set with | Default |
| --- | --- | --- | --- |
| "let me finish the answers I am holding" | your app | `SHUTDOWN_DRAIN_SECONDS` | `120` |
| "you have this long, then I kill you" | Kubernetes | `terminationGracePeriodSeconds` | `30` |

**When the platform's clock is the shorter one**

| Time | Your app | Kubernetes |
| --- | --- | --- |
| 0s | SIGTERM received, starts waiting for 1 run | starts a 30s timer |
| 20s | still writing the answer | 10s left |
| 30s | still writing | **SIGKILL** — the pod dies instantly |
| — | the answer is lost, exactly as before | — |

This one is nasty because **your logs say you handled it.** You see
`Waiting up to 120s for 1 run(s) to finish` and then nothing. The mechanism was
fine. The permission was not.

**With `terminationGracePeriodSeconds: 180`**

| Time | Your app | Kubernetes |
| --- | --- | --- |
| 0s | starts waiting | starts a 180s timer |
| 60s | answer saved, exits on its own | timer never fires |

```mermaid
flowchart TB
  T["SIGTERM — an answer needs 60 more seconds"] --> S{"how long will the platform wait?"}
  S -->|"30s, the default"| K["killed at 30s<br/>answer lost, log still looks fine"]
  S -->|"180s"| G["exits by itself at 60s<br/>answer saved, 120s never used"]
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `terminationGracePeriodSeconds` | Kubernetes manifest field. Time between `SIGTERM` and `SIGKILL`. | `30` | below the drain budget, every rollout kills answers halfway and the drain is decoration |
| `SHUTDOWN_DRAIN_SECONDS` | How long the app *wants* | `120` | keep it under the grace period, with slack — e.g. drain 120, grace 150–180 |

**Example.** The tell is a stopwatch, not a log line:

```bash
kubectl -n cfa get pod -w
```

A pod that exits at **exactly** the same second every time — 30.0, over and over
— was killed. A pod that exits at 41s, then 58s, then 33s, finished its work and
left. *At* the deadline means killed; *before* it means drained.

**Fix:** grace period above the drain budget, with room to spare.
**Yours — the manifest.** → [§7b](#7b-the-grace-period-rig-3)

---

#### 5 · Two writers, one message position

**Complaint:** *"The answer was on my screen. I refreshed and it was gone."*

**In one sentence.** Two messages try to take the same slot number in a
conversation, the database rejects one of them, and that rejection is
deliberately ignored — so an answer silently disappears.

**How messages are numbered.** Every message in a dossier gets the next number:
1, 2, 3… The database refuses two messages with the same number in the same
dossier (the `uq_message_position` constraint).

Saving a message is three steps:

1. ask the database: what is the highest number here? → **4**
2. add one → **5**
3. insert the row as number 5

**What goes wrong.** Two writers do step 1 at the same instant. Both are told 4.
Both try to insert 5.

| | Writer A | Writer B |
| --- | --- | --- |
| step 1 | highest is 4 | highest is 4 |
| step 2 | so mine is 5 | so mine is 5 |
| step 3 | INSERT 5 → **accepted** | INSERT 5 → **rejected** |

The rejection is then caught and thrown away *on purpose*: the code will not
fail a user's request over a numbering problem. So there is no error on screen,
no 500, nothing at INFO in the log. The analyst watched the answer arrive word
by word — it was streamed to the browser and never saved.

```mermaid
sequenceDiagram
  autonumber
  participant A as Writer A
  participant DB as Postgres
  participant B as Writer B
  A->>DB: highest number? → 4
  B->>DB: highest number? → 4
  A->>DB: INSERT 5 — accepted
  B->>DB: INSERT 5 — rejected
  Note over B: rejection swallowed on purpose<br/>the answer is simply gone
```

**The fix.** Lock the conversation row first, so steps 1–3 happen one writer at
a time. B waits, then reads 5 and takes 6. The lock lives in Postgres, so it
works between separate servers, not just inside one.

```mermaid
sequenceDiagram
  autonumber
  participant A as Writer A
  participant DB as Postgres
  participant B as Writer B
  A->>DB: lock this conversation
  B->>DB: lock this conversation — waits
  A->>DB: highest 4 → INSERT 5, done
  DB-->>B: your turn
  B->>DB: highest 5 → INSERT 6, done
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| *(none)* | There is no switch for this. The row lock is in the code and always on. | — | — |
| `DATABASE_URL` | Must point at Postgres — the lock and the unique constraint both live there | local Postgres | anything else and neither the constraint nor the lock exists |

**Example.** You do **not** need two servers. Open the same dossier in two
browser tabs and ask a question in both at once. Then count:

```sql
SELECT count(*) AS rows, max(seq) AS highest FROM messages
WHERE conversation_id = '…';
```

| rows | highest | verdict |
| --- | --- | --- |
| 8 | 8 | healthy |
| 7 | 8 | one message lost the race — that gap is somebody's answer |

More servers do not create this bug. They only make it ordinary: a filing upload
racing a question is enough, on any number of pods.

**Fix:** lock the conversation row while a number is being claimed.
**Handled** — verify by counting.
→ [§8](#8-break-5--two-writers-one-position)

---

#### 6 · Broadcasting

**Complaint:** none today — and that *is* the finding. This row exists so it
stays that way.

**In one sentence.** Nothing in the app sends a message to more than one browser
today, so nothing can break; the day someone does, only the viewers connected to
that *same* server will receive it.

**Two ways to send an event**

| Way | Who receives it | Works across servers? |
| --- | --- | --- |
| to one connection (`to=sid`) | the browser that asked | **yes, always** |
| to a room (a name others can join) | everyone who joined that name | **only those on the same server** |

Why: a server only knows about the browsers connected to *it*. Room membership
is a list in that server's memory. Pod A cannot reach a browser that is talking
to Pod B.

**Today** every event the app sends is addressed to the browser that asked —
streaming tokens, errors, the finished event. Nothing needs to cross between
servers, so there is nothing to configure and the app is correct at any number
of instances.

**The day it changes.** Someone adds a shared dossier, a "someone is typing"
dot, or a notification. It works perfectly in testing, because both your tabs
happened to land on the same pod. In production half the viewers see nothing,
and it reads as a flaky feature rather than a missing setting.

```mermaid
flowchart TB
  E1["today: send to one connection"] --> C1["that same browser — always right"]
  E2["someday: send to a room, from Pod A"] --> V1["viewer on Pod A — sees it"]
  E2 -.->|"never arrives"| V2["viewer on Pod B — sees nothing"]
  FIX["set SOCKETIO_MESSAGE_QUEUE_URL<br/>servers relay events through Redis"] --> V2
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `SOCKETIO_MESSAGE_QUEUE_URL` | Lets servers forward events to each other through Redis | *(empty — off)* | leaving it off is correct **today**; leaving it off after the first room-based feature means half your viewers silently miss events |
| `REDIS_URL` | Unrelated to the above — the message cache. Setting one does not set the other. | *(empty)* | — |

Turning the queue on costs a Redis round trip per event, which is why it is off
until something actually needs it.

**Example.** The whole test is one command, and today it prints nothing:

```bash
grep -rn "sio.emit" backend/Analyzer | grep -v "to=sid"
```

The day it prints a line, this row changes from "nothing to do" to "set the
message queue".

**Fix:** keep every event addressed to one connection; set the message queue
when that stops being possible. **Not yet — the test confirms it is still not
yet.** → [§9](#9-break-6--broadcasting-before-the-feature-exists)

---

#### 10 · Postgres connections

**Complaint:** *"FATAL: sorry, too many clients already"* — and it hits
everybody at once, including your own `psql`.

**In one sentence.** Every server keeps a handful of open lines to the database,
so enough servers will use up every line the database has — even while nobody is
using the app.

**What a connection is.** A request cannot talk to Postgres by itself. It
borrows one of a small set of already-open connections, uses it, and gives it
back. That set is the **pool**, and each server has its own.

**The arithmetic.** Each server holds 5 connections open all the time
(`DB_POOL_SIZE`), and may open up to 10 more when busy (`DB_MAX_OVERFLOW`) —
**15 per server at the worst moment**. Postgres has one limit for everyone
(`max_connections`, commonly 100).

| Servers | Worst case | Plus migrations and your `psql` | Fits under 100? |
| --- | --- | --- | --- |
| 2 | 30 | ~40 | yes |
| 4 | 60 | ~70 | yes |
| 8 | 120 | ~130 | **no** |

The ceiling is reached by **how many servers you run**, not by how busy they
are. Scale from 4 to 8 on a quiet afternoon and you can exhaust it having served
almost nothing.

**Why it is confusing when it happens.** The failure lands on whoever asks for a
connection *next*, which is rarely the cause: a health check fails, a migration
will not start, your `psql` is refused. The app itself can look fine.

```mermaid
flowchart TB
  P["8 servers x up to 15 connections each"] --> T["120 wanted"]
  T --> PG{"max_connections = 100"}
  PG -->|"first 100"| OK["served normally"]
  PG -->|"the rest"| X["too many clients already<br/>hits a health check, a migration, your psql"]
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `DB_POOL_SIZE` | Connections each server keeps open permanently | `5` | too high wastes the shared limit; too low queues requests behind each other |
| `DB_MAX_OVERFLOW` | Extra connections each server may open under load | `10` | this is the number that surprises people — the real per-server maximum is pool + overflow |
| `DB_POOL_RECYCLE_SECONDS` | Reopen a connection after this long | `1800` | too high and idle connections get dropped by a proxy or firewall without the app noticing |
| `DATABASE_URL` | Which database, as an async URL | local Postgres | a non-async URL is refused at startup, on purpose |
| `max_connections` | **Postgres server** setting, not an app one | often `100` | this is the ceiling all of the above must fit under |

**Example.** Do the arithmetic before the deployment does it for you:

```
servers x (DB_POOL_SIZE + DB_MAX_OVERFLOW) + room for migrations and psql
   8    x (      5      +       10       ) +  ~10  =  130  >  100   ✗
   4    x (      5      +       10       ) +  ~10  =   70  <  100   ✓
```

Then watch the real numbers while the tests run:

```sql
SELECT count(*) FROM pg_stat_activity;
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
```

The second query holds the trap. Any `idle in transaction` while a summary is
being written means a connection is **held across a model call** — 60 seconds of
a connection occupying a slot and doing nothing. That turns headroom that exists
on paper into exhaustion in practice.

**Fix:** size the pools so N servers fit under the limit, and never hold a
connection across a model call. **Yours.** → [§10](#10-postgres-connections)

---

#### 12 · Rolling summaries

**Complaint:** two identical "summarised" lines in the log, a minute of model
time spent twice.

**In one sentence.** Long conversations get squashed into a short summary, and
two servers can start squashing the same conversation at the same moment —
wasting a model call, but not breaking anything.

**What squashing is (folding).** The dossier keeps every message forever. What
is *sent to the model* is much smaller: the last few turns, plus a summary of
everything older. Once more than `HISTORY_SUMMARY_THRESHOLD` (24) messages are
unsummarised, the old ones are folded into that summary.

**What goes wrong.** Both servers see the count cross 24 at the same instant.
Both start a fold. Two identical model calls, about 30 seconds each, for one
result.

**Why it costs money and not correctness.** Each fold records how far it
summarised, in `summary_through_seq`. That number may only move **forwards** — a
fold that finishes late carrying an older number is thrown away rather than
applied. Two folds, one correct summary.

**So the coordination here is deliberately weak.** It is a *lease* in Redis: the
first server to claim it folds, the other skips. If Redis is missing, the lease
says yes to everybody and you are back to the occasional duplicate — which is
acceptable, precisely because the worst case is a wasted call. (Compare the
startup jobs in the next row, where being wrong is *not* acceptable, and which
therefore use a real lock in Postgres.)

```mermaid
flowchart TB
  T["message 24 lands — time to fold"] --> A["Pod A claims the lease — wins"]
  T --> B["Pod B tries the same lease — skips"]
  A --> M["one model call, about 30s"]
  M --> W["summary_through_seq = 20"]
  NR["no Redis: the lease says yes to everyone"] --> D["both fold — one wasted call<br/>the later, staler result is discarded"]
  D --> W
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `HISTORY_SUMMARY_THRESHOLD` | Unsummarised messages tolerated before a fold | `24` | low means constant folding (useful for testing, expensive in production); high means big prompts |
| `REDIS_URL` | Where the lease lives. Blank means no cross-server lease at all. | *(empty)* | duplicate folds become normal — wasteful, still correct |
| `HISTORY_CONTEXT_MESSAGES` | Recent turns sent to the model alongside the summary | `10` | too few loses the thread; too many makes every call slower |
| `HISTORY_CONTEXT_TOKENS` | Ceiling on that context | `1500` | too high and the model call slows down or truncates |

**Example.** Make folds happen in minutes instead of hours:

```bash
kubectl -n cfa set env deploy/cfa-backend HISTORY_SUMMARY_THRESHOLD=4
```

Ask six short questions in one dossier. Across **both** logs there should be
exactly one line per crossing:

```
INFO  conversations.service  Summarised … through seq 6 (4 message(s) folded)
```

Now the interesting half — stop Redis, restart both, and both servers fold.
Check the column anyway:

```sql
SELECT summary_through_seq, summary_tokens FROM conversations WHERE id = '…';
```

It still only moves forwards. You paid for one extra model call and got a
correct summary. That is the entire argument for using a lease here instead of a
lock. Put the threshold back afterwards, or every dossier you touch will fold
constantly.

**Fix:** a best-effort Redis lease, plus forward-only ordering that makes a
duplicate harmless. **Handled.**
→ [§11](#11-rolling-summaries-and-the-lease)

---

#### 6/7 · Housekeeping and the schema race

**Complaint:** *"First deploy of the day, and one pod is in CrashLoopBackOff."*

**In one sentence.** Some jobs should run once per deployment, not once per
server — and two servers creating the database tables at the same instant will
crash one of them.

**Which jobs.** Three things happen while a server boots:

1. create the tables if they are missing
2. delete filings nothing points at any more
3. sweep interrupted runs (the row above)

Every server doing 2 and 3 is wasteful — a lot of duplicate scanning at the
busiest moment in a server's life. Every server doing 1 is **fatal**: two
`CREATE TABLE` statements for the same table, one succeeds, the other gets
`DuplicateTable` and exits. Kubernetes restarts it, and it can happen again.

**The fix.** All of it runs inside a **Postgres advisory lock** — a lock with no
table behind it, used purely to mean "I am doing this, nobody else start". One
server wins and does the work. The others skip it, log a line saying so, and
start serving immediately; they do **not** wait.

This one uses a real lock, not the best-effort lease from the row above, because
here being wrong crashes a pod.

```mermaid
flowchart TB
  S["Pod A and Pod B boot in the same second"] --> L{"advisory lock — exactly one winner"}
  L -->|"Pod A wins"| W["create tables, prune filings, sweep runs"]
  L -->|"Pod B loses"| SK["logs 'another instance is doing the startup housekeeping'<br/>and starts serving right away"]
  NL["without the lock"] -.-> XX["both run CREATE TABLE<br/>one exits with DuplicateTable"]
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `DATABASE_URL` | Where the advisory lock lives, as well as the data | local Postgres | the lock is a Postgres feature; there is nowhere else for it to be |
| `STALE_RUN_MINUTES` | Used by the sweep that runs under this lock | `30` | see the sweep row above |
| *(no on/off switch)* | The lock is always taken; it cannot be disabled | — | — |

**Example.** On a **scratch** database, drop everything and start both servers
together:

```sql
DROP SCHEMA public CASCADE; CREATE SCHEMA public;
```

Pass: both pods reach Ready, no `DuplicateTable` anywhere, and across the two
logs exactly one line —

```
INFO  main  Another instance is doing the startup housekeeping
```

**One** is the number to check. Zero means both pods thought they had won and
the lock is doing nothing. Two means nobody did the work.

**Fix:** an advisory lock around the startup jobs. **Handled** — test the server
that skips as carefully as the one that wins.
→ [§12](#12-startup-housekeeping-and-the-schema-race)

---

#### 11 · Redis fail-soft

**Complaint:** there should not be one. That is the entire test.

**In one sentence.** Redis only makes reads faster, so losing it must make the
app slightly slower and nothing else — it must never break, and never crawl.

**What Redis holds here.** Two things, neither of which is the truth:

1. the recent messages of each conversation — a read cache in front of Postgres
2. the summary leases from the row above

Both can be rebuilt at any moment by reading Postgres. Nothing is only in Redis.

**The real risk is not losing the cache. It is the retrying.** If the app tried
to reconnect on every request while Redis was down, every request would sit
through a connection timeout first. A cache outage would have become a site
outage — the usual way an optional dependency quietly becomes a required one.

So each part switches itself off at the **first** error and stays off until the
process restarts.

| Redis | What one question costs | What the user notices |
| --- | --- | --- |
| up | one fast Redis read | nothing |
| down (what actually happens) | one extra `SELECT` | nothing |
| down, if it retried every time | a connection timeout, every request | everything hangs |

```mermaid
flowchart TB
  Q["a question arrives"] --> C{"is the cache switched on?"}
  C -->|"yes"| H["read recent messages from Redis"]
  C -->|"no"| P["read them from Postgres instead<br/>same answer, milliseconds slower"]
  E["Redis goes away"] --> F["first error switches the cache off<br/>and it stops trying"]
  F --> C
```

**The part people get wrong:** starting Redis again does **not** switch the
cache back on. The process has to restart.

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `REDIS_URL` | Where the cache and leases live. Blank means simply off. | *(empty)* | blank is a valid, correct deployment — just slower. A wrong URL costs one startup warning, then off |
| `REDIS_TTL_SECONDS` | How long a cached tail lives | `3600` | too short wastes the cache; too long only wastes memory, never correctness |
| `REDIS_HOT_WINDOW` | How many recent messages are kept per conversation | `40` | too small and most reads fall through to Postgres anyway |
| `REDIS_KEY_PREFIX` | Namespace for the keys | `cfa` | two deployments sharing one Redis will read each other's cache |

**Example.** Take it away completely and run the whole path — sign in, upload,
ask, reload, read the history back:

```bash
kubectl -n cfa scale deploy/redis --replicas=0
```

All of it must work. Two warnings, once each, are the receipt:

```
WARNING  conversations.cache  Message cache disabled after a Redis error: …
WARNING  core.leases          Leases disabled after a Redis error: …
```

Then bring it back — and restart the backends, or you will conclude the cache is
broken when it is only switched off:

```bash
kubectl -n cfa scale deploy/redis --replicas=1
kubectl -n cfa rollout restart deploy/cfa-backend   # <- without this it stays off
```

**Fix:** nothing to do. **Handled** — a deployment that *needs* Redis to be
correct has a bug in it. → [§13](#13-redis--proving-it-fails-soft)

---

#### 11 · Cache ordering

**Complaint:** *"The conversation looks out of order — but only sometimes, and
it fixes itself."*

**In one sentence.** Two servers writing to one conversation can add messages to
the cache in the wrong order, which is harmless because reads sort them — and
the thing to test is that the cache and the database always agree.

**Saving a message is two steps, in this order:**

1. write the row to **Postgres** — this is where its number comes from
2. append it to the cached list in **Redis**

Two servers run those two steps independently. Server B can lose the race on
step 1 and still win it on step 2.

| | Postgres (numbered) | Redis list (order it arrived) |
| --- | --- | --- |
| Pod A saves a message | `seq 3` | *(second)* |
| Pod B saves a message | `seq 4` | *(first)* |
| resulting cached list | — | **4, then 3** |

So the cached list genuinely is out of order. Nothing prevents that, and nothing
needs to.

**Why nobody ever sees it.** Reads sort by number before answering. The cached
answer and the database answer come out identical.

**So the property to test is not "the cache is in order".** It is **"the cache
and the database give the same answer"**. If they ever differ, the cache is
being treated as the truth somewhere it should not be — and that is a real bug,
not a cosmetic one.

```mermaid
flowchart TB
  A["Pod A saves seq 3"] --> L[("cached list, as stored:<br/>4, then 3")]
  B["Pod B saves seq 4"] --> L
  L --> S["reads sort by number"]
  PG[("Postgres, ordered by number:<br/>3, then 4")] --> S
  S --> O["3, then 4 — from either source, every time"]
```

**Settings involved**

| Setting | What it does | Default | Get it wrong and… |
| --- | --- | --- | --- |
| `REDIS_HOT_WINDOW` | How many recent messages the cached list keeps | `40` | smaller means more reads go to Postgres — slower, never wrong |
| `HISTORY_PAGE_SIZE` | Messages returned per page | `50` | a page larger than the hot window always falls through to the database |
| `HISTORY_MAX_PAGE_SIZE` | Ceiling a client may ask for | `200` | too high lets one request pull a huge page |
| `REDIS_URL` | Blank means no cache, so no ordering question at all | *(empty)* | — |

**Example.** Read the same dossier from both servers, then clear the cache and
read a third time:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/api/conversations/$SID/messages
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18002/api/conversations/$SID/messages
kubectl -n cfa exec deploy/redis -- redis-cli FLUSHALL
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/api/conversations/$SID/messages
```

All three responses identical and in number order; only the last is slower. If
the cached answer and the flushed answer differ, that is the real bug this test
exists to catch.

**Fix:** sort by number on read. **Handled** — cache and database agreeing is
the property. → [§14](#14-ordering-and-the-week-old-pod)

---

**The short version.** Five of these are **yours**, and all five are
configuration rather than code:

| # | Yours to set | Where |
| --- | --- | --- |
| 1 | `JWT_SECRET_KEY` — the same on every server | a Secret |
| 2 | `CHROMA_HOST` — one shared store | a Chroma server |
| 3 | sticky sessions | the ingress |
| 4 | `terminationGracePeriodSeconds` above the drain | the manifest |
| 10 | `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` × servers, under `max_connections` | arithmetic |

The rest are already handled in the code. What you are testing there is that
they *stay* handled — which is why each of them has a negative to run first:
take the handling away, watch it fail, put it back.

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
