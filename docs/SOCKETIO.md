# Socket.IO, from zero to production

A ground-up guide to the real-time layer of this app, written for someone who
has never used Socket.IO before.

It goes in three parts:

1. **[Part 1 — The ideas](#part-1--the-ideas)** — what Socket.IO is, what
   problem it solves, and the handful of words you need (`sid`, transport,
   handshake, event, namespace, room). No code from this repo yet.
2. **[Part 2 — This app](#part-2--this-app)** — every line of Socket.IO in
   `Corporate_Filing_Analyzer_Agent`, explained: how it is mounted onto
   FastAPI, how the connection is authenticated, how one answer streams back
   token by token.
3. **[Part 3 — Production](#part-3--production)** — nginx, timeouts, CORS,
   running more than one worker, and the list of things that break.

Behaviour of the app as a whole is in [HOW-IT-WORKS.md](HOW-IT-WORKS.md); setup
and configuration are in the [README](../README.md). This document is only
about the pipe.

---

## Contents

**Part 1 — The ideas**
- [Why not just HTTP?](#why-not-just-http)
- [What Socket.IO actually is](#what-socketio-actually-is)
- [The two layers: Engine.IO and Socket.IO](#the-two-layers-engineio-and-socketio)
- [The handshake, step by step](#the-handshake-step-by-step)
- [The vocabulary](#the-vocabulary)
- [Hello world in 40 lines](#hello-world-in-40-lines)

**Part 2 — This app**
- [How Socket.IO is mounted onto FastAPI](#how-socketio-is-mounted-onto-fastapi)
- [The one-line gotcha that drops every websocket](#the-one-line-gotcha-that-drops-every-websocket)
- [Authenticating the connection, not the message](#authenticating-the-connection-not-the-message)
- [The socket session](#the-socket-session)
- [The event contract](#the-event-contract)
- [Streaming one answer, end to end](#streaming-one-answer-end-to-end)
- [The client side](#the-client-side)
- [Why every event carries `session_id`](#why-every-event-carries-session_id)

**Part 3 — Production**
- [CORS, and how this app sidesteps it](#cors-and-how-this-app-sidesteps-it)
- [Putting nginx in front](#putting-nginx-in-front)
- [Timeouts and buffering](#timeouts-and-buffering)
- [Running more than one worker](#running-more-than-one-worker)
- [Rooms, and what you would use them for here](#rooms-and-what-you-would-use-them-for-here)
- [Reconnection, and what the client must assume](#reconnection-and-what-the-client-must-assume)
- [Debugging playbook](#debugging-playbook)
- [The gotcha checklist](#the-gotcha-checklist)
- [Cheat sheet](#cheat-sheet)

---
---

# Part 1 — The ideas

## Why not just HTTP?

Normal HTTP is a **question and an answer**. The browser asks, the server
replies, the connection closes. The server can never speak first.

That is fine for `GET /api/conversations`. It is wrong for this app's core
interaction, because an answer here is not one thing that arrives at one
moment. Asking *"what are the risk factors in this 10-K?"* sets off a run that
takes 30–90 seconds and produces a stream of things worth showing:

```
"I've started"  →  "I'm reading the filing"  →  "this is a risks question"
   →  "The"  "company"  "identifies"  "three"  …  →  "done"
```

With plain HTTP you would have to **poll** — the browser asking *"is it done
yet?"* every second, most answers being "no". That is wasteful, laggy, and
cannot deliver tokens smoothly.

What you want is a **connection that stays open in both directions**, so the
server can push whenever it has something. That is a **WebSocket**.

### So why not just use a raw WebSocket?

You can — FastAPI has `@app.websocket("/ws")` built in. But a raw WebSocket
gives you a bare pipe of bytes, and you end up rebuilding the same four things
every time:

| You need | Raw WebSocket gives you | Socket.IO gives you |
|---|---|---|
| Named messages (`token`, `done`, `error`) | Nothing — invent your own JSON envelope | `emit("token", {...})` / `on("token")` |
| Reconnecting when wifi drops | Nothing — write your own backoff loop | Automatic, with exponential backoff |
| Working where WebSockets are blocked | Fails | Falls back to HTTP long-polling |
| Knowing the connection is still alive | Nothing — write your own ping/pong | Built-in heartbeat |

Socket.IO is that layer, already written, with a matching client for the
browser. That is the whole reason it exists.

> **Rule of thumb.** Raw WebSocket if you control both ends, the network is
> friendly, and the message set is trivial. Socket.IO once you have a browser,
> real users on real networks, and more than two kinds of message. This app is
> squarely the second case.

---

## What Socket.IO actually is

Three things wearing one name:

1. **A protocol** — rules for encoding named events with JSON payloads.
2. **A server library** — here, [`python-socketio`](https://python-socketio.readthedocs.io/),
   pinned in [pyproject.toml](../backend/Analyzer/pyproject.toml) as
   `python-socketio>=5.12.1`.
3. **A client library** — here, `socket.io-client`, vendored into
   [frontend/socket.io.min.js](../frontend/socket.io.min.js) and loaded by a
   plain `<script>` tag (this frontend has no build step).

**The versions must match by major protocol version.** Socket.IO v4 client
talks to `python-socketio` 5.x. A v2 client against a v4 server does not
degrade gracefully — it fails the handshake outright with a confusing error.
This is the single most common "why won't it connect" cause in the wild.

Also worth saying plainly: **Socket.IO is not a WebSocket.** You cannot connect
to a Socket.IO server with `new WebSocket("ws://...")` or `wscat`. It *uses*
WebSocket as a transport, but wraps it in its own framing. You need a Socket.IO
client on the other end.

---

## The two layers: Engine.IO and Socket.IO

Socket.IO is built on a lower library called **Engine.IO**, and knowing the
split makes every error message legible.

```
┌───────────────────────────────────────────────┐
│  Your code                                    │
│    sio.emit("token", {"content": "The"})      │
├───────────────────────────────────────────────┤
│  Socket.IO      — events, namespaces, rooms,  │
│                   acks, auto-reconnect        │
├───────────────────────────────────────────────┤
│  Engine.IO      — "keep a connection alive    │
│                   somehow": transport choice, │
│                   upgrade, heartbeat          │
├───────────────────────────────────────────────┤
│  HTTP long-polling   ⇅   WebSocket            │
└───────────────────────────────────────────────┘
```

- **Engine.IO** answers *"how do I keep bytes flowing to this browser?"* It
  owns the transport, the upgrade from polling to WebSocket, and the
  ping/pong heartbeat.
- **Socket.IO** answers *"what do those bytes mean?"* — this is a `token`
  event, this is a `done` event, this one belongs to namespace `/`.

So when a stack trace or a log line says **`engineio`**, it is about the
*connection*; when it says **`socketio`**, it is about the *events*.

### The two transports

**HTTP long-polling** — the client sends a `GET`; the server holds it open
until it has something to say, then answers. The client immediately opens
another. Works absolutely everywhere, including through the most hostile
corporate proxy. Costs a round trip per message, which makes token streaming
visibly stutter.

**WebSocket** — one connection, upgraded from HTTP, that both sides can write
to freely. Fast, cheap, ideal for streaming. Occasionally blocked by old
proxies, and — importantly for Part 3 — **requires deliberate configuration in
any reverse proxy** in front of it.

Socket.IO's default behaviour is to **start on polling and upgrade to
WebSocket** once it has proved WebSocket works. That means a working
Socket.IO connection normally shows *two* entries in your browser's network
tab, which is not a bug.

This app's client asks for the opposite order:

```js
// frontend/app.js
transports: ["websocket", "polling"],
```

Try WebSocket first, fall back to polling only if it fails. That is the right
choice here: the payload is a token stream where the polling round trip is
felt immediately, and both deployments (dev direct, Docker behind nginx) are
known to allow the upgrade.

---

## The handshake, step by step

This is the sequence to have in your head. Everything in Part 3 is about
protecting one of these steps.

```
 CLIENT                                                   SERVER
   │
   │ ① GET /socket.io/?EIO=4&transport=polling
   │──────────────────────────────────────────────────────────▶
   │                                    (opens a connection,
   │                                     invents a sid,
   │                                     runs your connect handler)
   │ ◀──────────────────────────────────────────────────────────
   │   {"sid":"a1b2…","upgrades":["websocket"],
   │    "pingInterval":25000,"pingTimeout":20000}
   │
   │ ② GET /socket.io/?EIO=4&transport=websocket&sid=a1b2…
   │    Upgrade: websocket
   │──────────────────────────────────────────────────────────▶
   │ ◀───────────── 101 Switching Protocols ───────────────────
   │
   │   ═══ one open duplex connection from here on ═══
   │
   │ ③ emit("query", {...})   ──────────────────────────────▶
   │ ◀────────────────  emit("token", {...})  (many)
   │ ◀────────────────  emit("done",  {...})
   │
   │ ④ ping / pong every 25s, both ways
   │
   │ ⑤ close → client waits, then retries from ①
```

Four things to notice, because each becomes a production concern later:

- **`/socket.io/` is a real HTTP path.** Anything that routes HTTP — nginx, an
  ingress, a load balancer — must be told about it. That is why
  [frontend/nginx.conf](../frontend/nginx.conf) has a `location /socket.io/`
  block of its own.
- **`sid` is issued in step ①.** It identifies this connection. Step ② carries
  it back. If step ② lands on a *different server process* than step ①, that
  process has never heard of the `sid` and the handshake dies — this is the
  sticky-session problem, see [Running more than one
  worker](#running-more-than-one-worker).
- **Your `connect` handler runs during step ①**, before any event can be sent.
  That is the hook this app hangs authentication on.
- **The heartbeat is not optional.** A proxy that idles out a quiet connection
  will kill a run mid-answer. Hence the long `proxy_read_timeout`.

---

## The vocabulary

| Term | What it means |
|---|---|
| **`sid`** | Session id — a string naming *one connection*. New tab, new `sid`. Reconnect, new `sid`. Not a user id, and never assume it is stable. |
| **Event** | A named message: a name plus a JSON-serialisable payload. `sio.emit("token", {"content": "The"})`. |
| **`emit`** | Send an event. Fire-and-forget by default. |
| **`on` / `@sio.event`** | Register a handler for an incoming event name. |
| **Handshake** | The connection setup in steps ①–②. Where you accept or refuse. |
| **Transport** | Polling or WebSocket. See above. |
| **Namespace** | A logical channel on the same connection, path-like: `/`, `/admin`. This app uses only the default `/`. |
| **Room** | A named set of `sid`s you can emit to at once. `sio.enter_room(sid, "user:42")`. Server-side only — the client cannot join itself. |
| **Ack** | An optional callback fired when the other side has processed an event. Unused here — the reply flow is its own events. |
| **`environ`** | The raw ASGI/WSGI request dict for the handshake. Where you read headers, e.g. `HTTP_AUTHORIZATION`. |
| **`auth`** | A payload the client attaches to the handshake specifically for credentials. This app's primary token channel. |

Three reserved event names you do not invent: **`connect`**, **`disconnect`**,
**`connect_error`**.

---

## Hello world in 40 lines

Before touching this repo, here is the entire idea working. Two files.

**`server.py`**

```python
import socketio
from fastapi import FastAPI

# 1. A normal FastAPI app — nothing special.
app = FastAPI()

@app.get("/api/health")
async def health():
    return {"status": "ok"}

# 2. A Socket.IO server. async_mode="asgi" is what makes it speak
#    the same protocol FastAPI does.
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")

@sio.event
async def connect(sid, environ, auth):
    print("connected:", sid)

@sio.event
async def disconnect(sid):
    print("gone:", sid)

@sio.event
async def hello(sid, data):
    # Reply to just this one client with `to=sid`.
    await sio.emit("greeting", {"text": f"hi, you said {data['msg']}"}, to=sid)

# 3. Wrap them into ONE ASGI app. Socket.IO takes /socket.io/*,
#    everything else falls through to FastAPI.
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# run with:  uvicorn server:asgi_app --reload
```

**`index.html`**

```html
<script src="https://cdn.socket.io/4.8.1/socket.io.min.js"></script>
<script>
  const socket = io("http://localhost:8000");

  socket.on("connect",   () => socket.emit("hello", { msg: "world" }));
  socket.on("greeting",  (data) => console.log(data.text));
  socket.on("disconnect", () => console.log("dropped"));
</script>
```

That is genuinely the whole model: **`emit` on one side, `on` on the other, in
both directions, over a connection that stays open.** Everything in Part 2 is
this plus authentication, streaming, and bookkeeping.

---
---

# Part 2 — This app

## How Socket.IO is mounted onto FastAPI

All of it is at the bottom of
[backend/Analyzer/main.py](../backend/Analyzer/main.py):

```python
app = FastAPI(title="Corporate Filing Analyzer Agent API", ...)
app.include_router(auth_router)
app.include_router(chat_router)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=settings.CORS_ORIGINS if settings.CORS_ORIGINS != ["*"] else "*",
)
register_handlers(sio, chat_service, auth_service)

asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)
```

Read it as four moves:

1. **`FastAPI(...)`** — the ordinary HTTP app: `/api/auth/*`, `/api/upload`,
   `/api/conversations`, `/api/health`.
2. **`AsyncServer(async_mode="asgi")`** — the Socket.IO server. `asgi` is what
   makes it compatible with uvicorn and FastAPI. (Other modes exist for
   threading/eventlet/gevent stacks; none apply here, and mixing them is a
   common source of "it hangs" reports.)
3. **`register_handlers(...)`** — attaches this app's `connect` /
   `disconnect` / `query` handlers. They live in
   [api/socket_handler.py](../backend/Analyzer/api/socket_handler.py) rather
   than in `main.py`, so the wiring file stays about wiring.
4. **`ASGIApp(sio, other_asgi_app=app)`** — the composition step, and the one
   worth understanding properly.

### What `ASGIApp` actually does

It is a **router in front of both apps**:

```
             incoming request
                    │
          path starts with /socket.io/ ?
             ┌──────┴──────┐
            yes             no
             │               │
      Socket.IO server   the FastAPI app
      (sio)              (other_asgi_app)
```

That single object is what uvicorn serves. One process, one port, two
protocols. The browser sees `http://host/api/...` and `http://host/socket.io/...`
on the same origin, which — see Part 3 — is what makes CORS a non-problem in
the Docker deployment.

> **Note on services.** The Socket.IO handlers cannot use FastAPI's `Depends`
> — dependency injection is a FastAPI-routing feature and Socket.IO events do
> not go through FastAPI at all. So `register_handlers(sio, chat_service,
> auth_service)` passes the singletons in explicitly, taking them from
> [api/deps.py](../backend/Analyzer/api/deps.py), the same module the HTTP
> routes get them from. Same objects, different delivery mechanism.
>
> Likewise, database sessions are opened by hand inside the handler with
> `async with SessionLocal() as db:` rather than injected.

---

## The one-line gotcha that drops every websocket

```bash
uvicorn main:app         # ← REST works. Every socket 404s. No error anywhere.
uvicorn main:asgi_app    # ← correct
```

`app` is the FastAPI app, which knows nothing about `/socket.io/`. `asgi_app`
is the wrapper that knows about both. Serve the wrong one and the API looks
perfectly healthy while the chat is silently dead — the failure surfaces only
as `connect_error` in the browser console.

This is why the Dockerfile names it explicitly and comments on it:

```dockerfile
# `asgi_app` is the Socket.IO server wrapping the FastAPI app — mounting
# `main:app` instead would serve the REST API but drop every websocket.
CMD ["uvicorn", "main:asgi_app", "--host", "0.0.0.0", "--port", "8000", ...]
```

If real-time is broken and nothing is in the logs, **check this first.**

---

## Authenticating the connection, not the message

The design decision worth internalising: **this app authenticates the
connection, once, at the handshake** — not each `query` event.

Why that is better here:

- A connection with no valid token is **refused before it exists**, so no
  handler ever has to defend against an anonymous caller.
- The token is verified once instead of once per question.
- The resolved user is attached to the socket, so every query on that
  connection is answered for the account that opened it — the client cannot
  ask on someone else's behalf, because it never sends a user id at all.

From [api/socket_handler.py](../backend/Analyzer/api/socket_handler.py):

```python
@sio.event
async def connect(sid: str, environ: dict, auth_data: dict | None = None) -> None:
    token = _token_from(auth_data, environ)
    if not token:
        logger.info("Handshake from %s refused — no token", sid)
        raise socketio.exceptions.ConnectionRefusedError("Not signed in.")

    try:
        async with SessionLocal() as session:
            user = await auth.user_from_access_token(session, token)
    except AuthError as error:
        raise socketio.exceptions.ConnectionRefusedError(str(error)) from error

    await sio.save_session(sid, {"user_id": user.id, "email": user.email})
```

Three mechanics to note:

**`ConnectionRefusedError` is the API.** It is not a generic failure — raising
it is the documented way to reject a handshake, and the message travels to the
client's `connect_error` handler. Any *other* exception in `connect` also
refuses the connection, but with a generic message and a stack trace in your
logs; use the right one.

**The signature is `(sid, environ, auth)`.** `python-socketio` inspects the
handler and passes the third argument only if you accept it. Here it defaults
to `None` so the handler is safe either way.

**Two places to look for the token**, via `_token_from`:

```python
def _token_from(auth_data: dict | None, environ: dict) -> str:
    if auth_data:                                    # browser clients
        token = str(auth_data.get("token") or "").strip()
        if token:
            return token.removeprefix("Bearer ").strip()

    header = environ.get("HTTP_AUTHORIZATION", "")   # scripts, curl-likes
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""
```

The `auth` payload is the browser's channel — a browser **cannot set arbitrary
headers on a WebSocket handshake**, which is exactly why Socket.IO invented
`auth` in the first place. The `Authorization` header path exists for
non-browser clients that have no handshake payload to fill in. `environ` is the
raw request dict, so headers appear WSGI-style: `Authorization` →
`HTTP_AUTHORIZATION`.

### The matching client side

```js
// frontend/app.js
const socket = io(BACKEND_URL, {
  transports: ["websocket", "polling"],
  reconnectionAttempts: 20,
  reconnectionDelay: 1500,
  autoConnect: false,
  auth: (cb) => cb({ token: Auth.accessToken }),
});
```

- **`autoConnect: false`** — there is nothing to connect *for* until someone is
  signed in. `startSession()` calls `socket.connect()` after sign-in;
  `endSession()` calls `socket.disconnect()` on sign-out.
- **`auth` is a callback, not an object.** This matters more than it looks.
  A fixed object is read once, at construction. A callback is invoked on
  **every** attempt, including every reconnection — so a socket that drops and
  comes back an hour later hands over the token that is current *then*, not the
  expired one it was first opened with.

And the failure path distinguishes the two ways a handshake can fail:

```js
socket.on("connect_error", async (error) => {
  const rejected = /sign|token|session|expire/i.test(error?.message || "");

  if (rejected && Auth.isSignedIn) {
    try {
      await Auth.refresh();
      socket.connect();      // the auth callback picks up the new token
      return;
    } catch { /* refresh token dead too — Auth drops the session */ }
  }

  if (!offline) showToast("Can't reach the analyzer — retrying…", "error");
  offline = true;
});
```

**Rejected** (bad/expired token) and **unreachable** (server down) need
opposite responses. Retrying an expired token just gets refused again forever;
refreshing first and retrying fixes it. Retrying an unreachable server *does*
help, so that path just toasts and lets the built-in backoff work.

---

## The socket session

`python-socketio` gives every connection a small server-side dict:

```python
await sio.save_session(sid, {"user_id": user.id, "email": user.email})
...
socket_session = await sio.get_session(sid)
user_id = socket_session.get("user_id") if socket_session else None
```

Think of it as **server-side session storage keyed by connection**. The client
can neither read nor write it — which is the whole point. The user id used to
scope retrieval and history comes from here, never from the event payload.

The `query` handler still checks it defensively:

```python
if not user_id:
    await sio.emit("error", {"message": "Your session expired — sign in again.", ...}, to=sid)
    return
```

That should be unreachable after a successful handshake. It exists because the
consequence of being wrong is answering a question with no owner.

> **Caveat for later:** by default this session lives in the process's memory.
> With multiple workers it does not travel — see [Running more than one
> worker](#running-more-than-one-worker).

---

## The event contract

Client → server: exactly one event.

| Event | Payload |
|---|---|
| `query` | `{ query, session_id, title, files[] }` |

- `query` — the analyst's question. May be empty when a filing was attached
  without one; the server substitutes `FALLBACK_QUERY` so the router still has
  something to classify.
- `session_id` — the dossier. Scopes retrieval to that dossier's uploads and
  names the conversation the run is written into. **Refused if missing** — a
  query with no dossier behind it has no filings it is entitled to read.
- `title` — the name the dossier already carries, blank if it has none. Sending
  it is what stops the analyzer renaming the dossier on every run.
- `files[]` — names of filings attached to this question, recorded alongside
  it so a reopened dossier still shows what a run was asked against. Capped at
  20 server-side.

Server → client: six events, produced by `ChatService.query_stream` and
forwarded by the handler.

| Event | Payload | Meaning |
|---|---|---|
| `run_started` | `{ run_id }` | The run exists; here is its id. |
| `status` | `{ stage }` — `retrieve` / `route` / `analyze` | Progress, translated by the UI into plain language ("reading the filing"). |
| `title` | `{ title }` | The analyzer named this dossier. |
| `route` | `{ category }` | What kind of question this was judged to be. |
| `token` | `{ content }` | One fragment of the answer. Many of these. |
| `done` | `{ run_id, category, title }` | Finished. |
| `error` | `{ message }` | It did not finish. Terminal, like `done`. |

**Every one of them also carries `session_id`.** More on why in a moment.

---

## Streaming one answer, end to end

The `query` handler is the spine of the app. Here is the shape, with the
reasoning behind each stage.

### 1. Read the payload, resolve the user

```python
question = (data.get("query") or "").strip() or FALLBACK_QUERY
session_id = (data.get("session_id") or "").strip()
title = (data.get("title") or "").strip()
attachments = [str(name) for name in (data.get("files") or [])][:20]

socket_session = await sio.get_session(sid)
user_id = socket_session.get("user_id") if socket_session else None
```

Payload data is treated as untrusted input — coerced, stripped, bounded.
Identity comes from the socket session, not the payload.

### 2. Open the ledger before the run starts

```python
async with SessionLocal() as db:
    conversation = await history_service.open_conversation(db, user_id, session_id, title)
    conversation_pk = conversation.id
    title = conversation.title or title          # the stored name wins

    history = await history_service.context_for(db, conversation)
    await history_service.record_message(db, conversation, ROLE_USER, question, ...)
```

Two subtleties, both commented in the source:

- **History is assembled *before* the question is recorded.** Otherwise the
  question being asked right now would also appear in the prior-context block,
  arriving in the prompt twice.
- **The stored title wins over the client's.** The server named the dossier; a
  client that has fallen behind should not be able to rename it.

### 3. Stream, forwarding as you go

```python
async for event in chat.query_stream(question, session_id=..., title=..., history=...):
    payload = dict(event)
    event_name = payload.pop("event")
    payload["session_id"] = session_id

    if event_name == "token":
        answer.append(payload.get("content", ""))
    elif event_name == "run_started":
        run_id = payload.get("run_id", "")
    elif event_name in {"route", "done"}:
        category = payload.get("category") or category
        title = payload.get("title") or title

    await sio.emit(event_name, payload, to=sid)
```

`query_stream` is an **async generator** yielding dicts like
`{"event": "token", "content": "The"}`. The handler pops `event` to use as the
Socket.IO event *name*, stamps the dossier id onto what remains, and emits it.

Two habits worth copying:

- **`to=sid`** — send to this one connection. Omit it and you broadcast to
  every connected client, which here would mean sending one analyst's filing
  analysis to everybody. `to=` is not optional politeness; it is the access
  control.
- **The answer is accumulated as it flies past**, so what gets written to the
  ledger is byte-for-byte what the analyst was shown — rather than
  re-generating or reconstructing it afterwards.

Because the whole thing is `async` and awaits at each step, uvicorn's event
loop stays free: one slow run does not block anyone else's.

### 4. Record the outcome — always

```python
except Exception as error:
    logger.exception("Query from %s failed", sid)
    failure = str(error)
    await sio.emit("error", {"message": failure, "session_id": session_id}, to=sid)

await _record_answer(conversation_pk, "".join(answer), ..., failure=failure)
```

`_record_answer` sits **outside** the `try`, so a failed run is still written
to the ledger, marked as failed. The analyst should come back tomorrow and see
that the question was asked and did not land — not find it missing. And
`_record_answer` never raises: losing the record of an answer already read is
not worth turning into an error the analyst has to act on.

### Short-lived database sessions, on purpose

Notice there are **three separate `async with SessionLocal()` blocks** — one to
open the ledger, one inside `_record_answer`, none during the stream itself.
This is deliberate and is the module docstring's point:

> an answer can take a minute, and a pooled connection held open across it is a
> connection nothing else can use.

Holding a DB session open for the duration of an LLM run is a classic way to
exhaust a connection pool under mild load. Open late, close early.

---

## The client side

The receiving half, from [frontend/app.js](../frontend/app.js):

```js
socket.on("run_started", (data) => { const t = liveTurn(data); if (t) t.runId = data.run_id; });

socket.on("status", (data) => {
  const turn = liveTurn(data);
  if (!turn) return;
  if (data.stage === "retrieve") setWork(turn, "reading the filing");
  if (data.stage === "route")    setWork(turn, "working out what you're asking");
  if (data.stage === "analyze")  setWork(turn, `writing the ${labelOf(data.category).toLowerCase()} answer`);
});

socket.on("token", (data) => {
  const turn = liveTurn(data);
  if (!turn || !data.content) return;
  clearWork(turn);
  turn.raw += data.content;                    // accumulate the raw markdown
  turn.body.innerHTML = marked.parse(turn.raw);  // re-render each token
  scrollToBottom();
});

socket.on("done", (data) => { /* … close the run out … */ });
socket.on("error", (data) => { /* … show the fault … */ });
```

`token` accumulates into `turn.raw` and re-parses the markdown each time, so
half-written tables and code fences render correctly as they complete rather
than flickering as broken markup.

And sending is one call:

```js
socket.emit("query", {
  query: text,
  session_id: turn.sessionId,
  title: dossier.title,
  files: files.map((f) => f.name),
});
```

Note `turn.sessionId`, not the currently-active dossier: a run asks of the
dossier it was opened in, even if the analyst switches away a second later.

---

## Why every event carries `session_id`

This is the most instructive detail in the whole real-time layer, and it
generalises to any streaming UI you build.

The problem: the analyst asks a question in dossier A, waits, gets bored, and
clicks over to dossier B. Twenty seconds later dossier A's tokens start
arriving. **Where do they go?** Naively — into whatever is on screen, which is
now dossier B. The analyst watches an answer about one company's filing being
typed into an unrelated conversation.

The connection cannot help you here: it is one socket, shared by every dossier.
So the *events* have to carry their own address:

```python
payload["session_id"] = session_id       # server: stamp every event
```

```js
function liveTurn(data) {
  const turn = state.turn;
  if (!turn || turn.sessionId !== state.active.id) return null;   // run isn't the one on screen
  if (data?.session_id && data.session_id !== state.active.id) return null;  // event isn't for this dossier
  return turn;
}
```

Every handler starts with `liveTurn(data)` and returns early on `null`. Late
events from an abandoned run are dropped rather than written into whatever
happens to be on screen.

Two refinements worth stealing:

- **`done` and `error` are terminal**, so a stale one must return *before*
  `finishTurn()` — otherwise a dead run would end whichever run is live now.
- **`done` calls `nameDossier(data)` before the staleness check.** A name is
  worth keeping even when it belongs to a dossier the analyst has moved on
  from; it is addressed by the id in the event, not by what is on screen.

And the id that goes back to the browser is the **client's own** `session_id`,
not the internal `scoped_session_id(user_id, session_id)` used for the vector
store. The account a dossier is scoped under on the backend is not the
browser's business.

> **The general lesson.** On a multiplexed connection, the connection tells you
> *who*; it cannot tell you *which*. Anything that can outlive its context on
> screen must carry its own correlation id, and the receiver must be willing to
> throw events away.

---
---

# Part 3 — Production

## CORS, and how this app sidesteps it

Socket.IO's handshake is an ordinary HTTP request, so a browser applies the
same-origin rules to it. Cross-origin means the server must say yes:

```python
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=settings.CORS_ORIGINS if settings.CORS_ORIGINS != ["*"] else "*",
)
```

Two independent CORS settings exist in this app and they are easy to confuse:

| Setting | Governs |
|---|---|
| FastAPI's `CORSMiddleware` | `/api/*` HTTP routes |
| `AsyncServer(cors_allowed_origins=...)` | the `/socket.io/` handshake |

**Socket.IO does not inherit FastAPI's middleware** — the requests never reach
FastAPI, they are routed away by `ASGIApp`. Configure both. A REST API that
works while the socket is refused with a CORS error in the console is almost
always this.

The best fix is to not be cross-origin at all, which is what the Docker
deployment does: nginx serves the page *and* proxies `/api` and `/socket.io` to
the backend, so the browser only ever sees one origin. From
[frontend/config.docker.js](../frontend/config.docker.js):

```js
window.__BACKEND_URL__ = "";   // "" → this page's own origin
```

The preflight, the credentials rules and the handshake all stop being
cross-origin problems simultaneously. In dev, run direct against
`http://localhost:8000` and set `CORS_ORIGINS` accordingly.

---

## Putting nginx in front

The WebSocket-specific part of [frontend/nginx.conf](../frontend/nginx.conf):

```nginx
location /socket.io/ {
    proxy_pass         http://backend:8000;
    proxy_http_version 1.1;

    # The websocket upgrade. Without these two the transport silently falls
    # back to long-polling, and every answer arrives in stutters.
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering    off;
}
```

Line by line, because every one of these is load-bearing:

- **`proxy_http_version 1.1`** — nginx proxies with HTTP/1.0 by default, and
  the WebSocket upgrade does not exist in 1.0. Without this the upgrade cannot
  happen at all.
- **`Upgrade` / `Connection`** — the two headers that carry the upgrade. Miss
  them and there is no error: the client quietly falls back to polling and the
  app *works*, just badly. **This is the failure mode to know**, because
  "everything works but streaming stutters" rarely gets diagnosed as a proxy
  config problem.
- **`proxy_read_timeout 3600s`** — a long answer streams with quiet stretches
  in between. nginx's default is 60s, which would sever the connection
  mid-answer. Must exceed your longest expected silence.
- **`proxy_buffering off`** — buffering collects the response before forwarding
  it, which is precisely wrong for a token stream: tokens would arrive in
  clumps, or all at once at the end. **Non-negotiable for streaming.**
- **`X-Forwarded-*`** — so the backend sees the real client, paired with
  uvicorn's `--proxy-headers --forwarded-allow-ips "*"` in the Dockerfile.

Note the `/api/` block has its own, different timeout (`300s`) and
`proxy_request_buffering off`, for a different reason: uploads. A 10-K PDF is
embedded synchronously and that is not fast on a local model.

### Other proxies

- **Traefik / Caddy** — handle WebSocket upgrades automatically; you mainly
  need to raise the read timeout.
- **AWS ALB** — set idle timeout above your longest quiet stretch (default 60s)
  and enable stickiness if you run more than one target.
- **Cloudflare** — WebSockets work on all plans, but the 100s proxy timeout
  applies to *idle* connections. The Socket.IO heartbeat (25s default) keeps
  them alive; do not disable it.
- **Kubernetes ingress-nginx** — the annotations
  `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` and
  `.../affinity: "cookie"` are the two you will need.

---

## Timeouts and buffering

Four timeouts sit on the path, and the connection dies at the shortest.

| Layer | Setting | Here |
|---|---|---|
| Socket.IO heartbeat | `ping_interval` / `ping_timeout` | 25s / 20s (defaults) |
| nginx | `proxy_read_timeout` | 3600s |
| Cloud LB, if any | idle timeout | raise above the heartbeat |
| Client | `reconnectionDelay`, `reconnectionAttempts` | 1500ms, 20 attempts |

Rule: **every proxy timeout on the path must be comfortably longer than the
heartbeat interval.** If a proxy idles out in 30s and the heartbeat is 25s, you
are betting on jitter and will lose intermittently.

If you have a long silent stage — say retrieval on a very large filing — you
can widen the server heartbeat:

```python
sio = socketio.AsyncServer(async_mode="asgi", ping_interval=25, ping_timeout=60)
```

Prefer emitting a `status` event instead. It keeps the connection warm *and*
tells the analyst something, which is what this app does at each graph stage.

---

## Running more than one worker

Today the backend runs a single uvicorn process:

```dockerfile
CMD ["uvicorn", "main:asgi_app", "--host", "0.0.0.0", "--port", "8000", ...]
```

No `--workers`. That is correct as it stands, and you should understand why
before changing it.

**Two things break with multiple processes:**

**1. The handshake splits.** Step ① issues a `sid` in worker A; step ② arrives
at worker B, which has never heard of that `sid`. The result is an endless
connect/disconnect loop.

*Fixes:* sticky sessions at the proxy (route by `io` cookie or client IP), or
force `transports: ["websocket"]` only — a WebSocket-only client makes the
whole handshake one connection that cannot split. The second is simpler but
gives up the polling fallback.

**2. `emit` only reaches your own process.** Worker A cannot emit to a `sid`
connected to worker B. It fails silently, which is the worst kind.

*Fix:* give the servers a shared message bus:

```python
mgr = socketio.AsyncRedisManager("redis://redis:6379/0")
sio = socketio.AsyncServer(async_mode="asgi", client_manager=mgr)
```

Every worker then publishes emits through Redis and delivers what belongs to
its own clients. Redis is already in
[docker-compose.yml](../docker-compose.yml) for the message cache, so the
building block is there.

Note that `sio.save_session()` is still per-process memory. In this app that is
fine even with the manager, because a session is only ever read by the worker
holding the connection — but do not rely on reading another worker's session.

**Should you scale out?** For this workload, probably not first. The bottleneck
is Ollama, not the socket layer — every worker would queue on the same model.
Scale the model host before the API. When you do scale the API: Redis manager
plus sticky sessions, both.

---

## Rooms, and what you would use them for here

A **room** is a named set of `sid`s:

```python
await sio.enter_room(sid, f"user:{user.id}")
await sio.emit("notice", {...}, room=f"user:{user.id}")   # every device of that user
await sio.leave_room(sid, f"user:{user.id}")
```

Rooms are server-side only — a client cannot join one by asking, which is what
makes them safe as an authorization boundary.

This app **does not use them**, and does not need to: every emit is `to=sid`,
answering the one connection that asked. `to=sid` is in fact a room of one —
each connection is automatically in a room named after its own `sid`.

Where rooms would earn their place here, if the app grew:

- **The same analyst on two devices.** Join `user:{id}` at connect, emit to the
  room, and an answer appears on the laptop and the tablet together.
- **Shared dossiers.** Room per dossier, so colleagues watching the same
  dossier see a run stream live.
- **Admin broadcast.** "Maintenance in 5 minutes" to everyone.

Each would be a few lines — join in `connect`, swap `to=sid` for
`room=...` — but each also changes who is allowed to see an answer, so it is a
security decision, not a plumbing one.

---

## Reconnection, and what the client must assume

Socket.IO reconnects automatically. Here:

```js
reconnectionAttempts: 20,
reconnectionDelay: 1500,
```

Twenty attempts with growing backoff, each running the `auth` callback afresh
so a token refreshed in the meantime is picked up.

**What reconnection does not do: resume a run.** A new connection is a new
`sid`, and the server-side generator streaming your answer died with the old
one. Tokens already emitted are gone.

This app is honest about that:

```js
socket.on("disconnect", () => {
  if (state.busy) {
    showToast("Connection lost — the run was interrupted", "error");
    finishTurn();
  }
});
```

The run is closed out and the analyst is told. Crucially, the *ledger* is not
lost — the question was recorded before the run started, so it is still there
on reload, and the analyst can ask again.

If you needed true resumability you would have to buffer emitted tokens
server-side against the `run_id` and let a reconnecting client ask for what it
missed. That is a real feature with real cost, not a Socket.IO setting.

---

## Debugging playbook

Work outward from the browser.

**1. Is the handshake happening?** Network tab → filter `socket.io`. You want
a `200` polling request then a `101 Switching Protocols`.

| What you see | Likely cause |
|---|---|
| `404` on `/socket.io/` | Serving `main:app`, or the proxy has no `/socket.io/` route |
| `403` / CORS error | `cors_allowed_origins` does not include your origin |
| `200` polling but never a `101` | Missing `Upgrade`/`Connection` headers, or `proxy_http_version 1.1` |
| `connect_error` with your own message | Auth refusal — working as designed |
| Endless connect/disconnect | Multiple workers with no sticky sessions |

**2. Turn on client logging.** In the browser console:

```js
localStorage.debug = "socket.io-client:*,engine.io-client:*";
```

then reload. `engine.io-client` lines are transport problems;
`socket.io-client` lines are event problems.

**3. Turn on server logging.**

```python
sio = socketio.AsyncServer(async_mode="asgi", logger=True, engineio_logger=True)
```

Verbose — for a debugging session, not for production.

**4. Test the server without the browser.**

```bash
pip install "python-socketio[asyncio_client]"
```

```python
import asyncio, socketio
sio = socketio.AsyncClient()

@sio.on("token")
async def on_token(data): print(data["content"], end="", flush=True)

async def main():
    await sio.connect("http://localhost:8000", auth={"token": "<paste access token>"})
    await sio.emit("query", {"query": "summarise the filing", "session_id": "abc123", "title": ""})
    await sio.wait()

asyncio.run(main())
```

This cleanly separates "the backend is broken" from "the frontend is broken".

**5. Check the app's own logs.** The handlers log every interesting decision:

```
Client connected: xY9… (user=analyst@example.com)
Handshake from xY9… refused — no token
Query from xY9… (chat=abc123): 'what are the risk factors'
Query from xY9… failed
```

---

## The gotcha checklist

Run down this list when real-time misbehaves.

- [ ] **Serving `main:asgi_app`, not `main:app`.** Silent, total socket failure.
- [ ] **Client and server major versions match** — v4 client, `python-socketio` 5.x.
- [ ] **`cors_allowed_origins` set on the Socket.IO server**, separately from FastAPI's middleware.
- [ ] **Proxy passes `Upgrade`/`Connection` and uses HTTP/1.1.** Otherwise: silent polling fallback.
- [ ] **`proxy_buffering off`.** Otherwise tokens arrive in clumps.
- [ ] **Proxy read timeout > longest quiet stretch.** Otherwise: cut off mid-answer.
- [ ] **`to=sid` on every emit.** Omitting it broadcasts to everyone.
- [ ] **No long-held DB session across a stream.** Open late, close early.
- [ ] **One worker, or Redis manager + sticky sessions.** Never multiple workers with neither.
- [ ] **Every event carries a correlation id** and the client drops stale ones.
- [ ] **`auth` is a callback**, so reconnects carry a fresh token.
- [ ] **Handlers are `async` and never block.** A synchronous sleep or a blocking DB driver stalls every other client on the loop.
- [ ] **Payloads are JSON-serialisable.** No `datetime`, no `UUID`, no SQLModel objects — serialise them yourself.
- [ ] **Exceptions inside a handler are caught.** An uncaught one kills the run with no message to the client.

---

## Cheat sheet

**Server — `python-socketio`**

```python
import socketio

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
asgi_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)

@sio.event
async def connect(sid, environ, auth): ...      # raise ConnectionRefusedError to reject
@sio.event
async def disconnect(sid): ...
@sio.event
async def my_event(sid, data): ...              # name of the function = name of the event

@sio.on("kebab-case-name")                      # for names that aren't valid identifiers
async def handler(sid, data): ...

await sio.emit("name", payload, to=sid)         # one client
await sio.emit("name", payload, room="r")       # a room
await sio.emit("name", payload)                 # EVERYONE — rarely what you want
await sio.emit("name", payload, room="r", skip_sid=sid)   # a room, except the sender

await sio.save_session(sid, {...})
session = await sio.get_session(sid)
await sio.enter_room(sid, "r")
await sio.leave_room(sid, "r")
await sio.disconnect(sid)
```

**Client — `socket.io-client`**

```js
const socket = io(URL, {
  transports: ["websocket", "polling"],
  autoConnect: false,
  reconnectionAttempts: 20,
  reconnectionDelay: 1500,
  auth: (cb) => cb({ token: getToken() }),
});

socket.connect();
socket.disconnect();

socket.on("connect",       () => {});
socket.on("disconnect",    (reason) => {});
socket.on("connect_error", (err) => {});
socket.on("my_event",      (data) => {});

socket.emit("my_event", { ... });

socket.connected   // boolean
socket.id          // the sid, only while connected
```

**This app's map**

| File | What lives there |
|---|---|
| [backend/Analyzer/main.py](../backend/Analyzer/main.py) | `AsyncServer`, `ASGIApp`, CORS, the mount |
| [backend/Analyzer/api/socket_handler.py](../backend/Analyzer/api/socket_handler.py) | `connect` / `disconnect` / `query`, auth, streaming, ledger writes |
| [backend/Analyzer/api/deps.py](../backend/Analyzer/api/deps.py) | the singleton services passed into the handlers |
| [backend/Analyzer/services/chat_service.py](../backend/Analyzer/services/chat_service.py) | `query_stream` — the async generator behind every event |
| [frontend/app.js](../frontend/app.js) | client construction, auth callback, all six `socket.on` handlers, the one `emit` |
| [frontend/nginx.conf](../frontend/nginx.conf) | the `/socket.io/` proxy block |
| [backend/Dockerfile](../backend/Dockerfile) | `uvicorn main:asgi_app` |

---

## Where to go next

- [python-socketio docs](https://python-socketio.readthedocs.io/) — the server
  library's own reference; the "Server" and "Deployment" pages are the useful ones.
- [Socket.IO client docs](https://socket.io/docs/v4/client-api/) — the
  client API, and the "Troubleshooting connection issues" page in particular.
- [HOW-IT-WORKS.md](HOW-IT-WORKS.md) — what happens around the socket layer:
  the graph, the ledger, the vector store.
