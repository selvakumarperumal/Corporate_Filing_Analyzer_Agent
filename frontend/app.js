/**
 * Corporate Filing Analyzer — Analyst Workbench client
 *
 * Talks to the FastAPI + LangGraph backend over Socket.IO. Each question is a
 * numbered run in the ledger: the question, the answer, the kind of analysis
 * that produced it, and the passages it drew on.
 *
 * The pipeline behind an answer (retrieval, classification, node dispatch) is
 * deliberately not surfaced. The UI shows plain-language progress while a run
 * is in flight and nothing about graph internals once it lands — the one
 * exception is the category tag, which tells the reader what kind of answer
 * they are looking at.
 */

// ── Configuration ────────────────────────────────────────────────────────
const BACKEND_URL = (() => {
  // Set by config.js. The Docker image writes an empty string there, meaning
  // this page's own origin — nginx proxies /api and /socket.io to the backend,
  // so there is no second origin to point at.
  if (typeof window.__BACKEND_URL__ === "string") {
    return window.__BACKEND_URL__ || window.location.origin;
  }
  // Started by hand: the backend is its own origin on port 8000.
  return window.location.port === "8000"
    ? window.location.origin
    : "http://localhost:8000";
})();

// The kind of analysis an answer came out of, shown as a tag on the run.
const CATEGORIES = [
  { id: "financials",   label: "Financials",   tone: "jade"   },
  { id: "compliance",   label: "Compliance",   tone: "blue"   },
  { id: "risks",        label: "Risks",        tone: "rose"   },
  { id: "shareholding", label: "Shareholding", tone: "violet" },
  { id: "governance",   label: "Governance",   tone: "cyan"   },
  { id: "mda",          label: "MD&A",         tone: "gold"   },
  { id: "summary",      label: "Summary",      tone: "amber"  },
  { id: "qa",           label: "Q&A",          tone: "slate"  },
];

const CATEGORY_BY_ID = Object.fromEntries(CATEGORIES.map((c) => [c.id, c]));
const ACCEPTED_EXT = [".pdf", ".txt", ".md", ".csv"];

if (typeof marked !== "undefined") {
  marked.setOptions({ breaks: true, gfm: true });
}

// ── State ────────────────────────────────────────────────────────────────
const state = {
  dossiers: [],  // every dossier opened this session, in the order they opened
  active: null,  // the one on the stage
  busy: false,
  turn: null,    // the run currently on the wire
};

/**
 * A dossier owns everything about one line of enquiry: the filings it may
 * read, the runs it has answered, and the name the analyzer gave it. Only the
 * open one is on the stage — the rest wait in the dock with their ledger
 * intact, so going back to one is going back to where it was left.
 *
 * The backend keeps the same three things, so a dossier also survives the
 * browser: `loaded` and `earlier` are what a dossier restored from the ledger
 * uses to fill its stack in, a page at a time, the first time it is opened.
 */
function makeDossier() {
  const stack = document.createElement("div");
  stack.className = "run-stack";
  return {
    id: newId(),
    title: "",    // named by the analyzer on the first question it answers
    runNo: 0,
    pending: [],  // filings staged in the command bar
    indexed: [],  // filings ingested into this dossier's own collection
    stack,        // its ledger entries, detached while another dossier is open
    loaded: true, // whether its stack matches what the backend has stored
    earlier: null,// cursor for the page before the oldest run on the stack
    loading: false,
  };
}

const dossierById = (id) => state.dossiers.find((d) => d.id === id) || null;

// ── DOM ──────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const chatContainer = $("chatContainer");
const messagesList = $("messagesList");
const welcomeHero = $("welcomeHero");
const userInput = $("userInput");
const sendBtn = $("sendBtn");
const attachBtn = $("attachBtn");
const attachTray = $("attachTray");
const fileInput = $("fileInput");
const uploadedFilesList = $("uploadedFilesList");
const fileCountBadge = $("fileCountBadge");
const clearChatBtn = $("clearChatBtn");
const newChatBtn = $("newChatBtn");
const dossierList = $("dossierList");
const dossierCountBadge = $("dossierCountBadge");
const dossierTitleEl = $("dossierTitle");
const dock = $("dock");
const dockToggle = $("dockToggle");
const dockClose = $("dockClose");
const scrim = $("scrim");
const toastContainer = $("toastContainer");
const sessionIdEl = $("sessionId");
const dragOverlay = $("dragOverlay");
const userInitial = $("userInitial");
const userName = $("userName");
const userEmail = $("userEmail");
const logoutBtn = $("logoutBtn");

// ── Socket.IO ────────────────────────────────────────────────────────────
/**
 * The connection carries the analyst's identity: the backend refuses a
 * handshake without a valid access token, so there is nothing to connect for
 * until one exists — hence `autoConnect: false`, with `startSession()` opening
 * it once signed in.
 *
 * `auth` is a callback rather than a fixed object because it is invoked on
 * every attempt, including reconnections. A socket that drops and comes back
 * an hour later therefore hands over the token current at that moment, not the
 * expired one it was first opened with.
 */
const socket = io(BACKEND_URL, {
  transports: ["websocket", "polling"],
  reconnectionAttempts: 20,
  reconnectionDelay: 1500,
  autoConnect: false,
  auth: (cb) => cb({ token: Auth.accessToken }),
});

/**
 * Connection state is only worth the analyst's attention when it breaks, so it
 * is raised as a toast rather than parked on screen. `offline` keeps a failed
 * connection from re-toasting on every reconnection attempt.
 */
let offline = false;

socket.on("connect", () => {
  if (offline) showToast("Reconnected", "success");
  offline = false;
});

socket.on("disconnect", () => {
  if (state.busy) {
    if (state.turn) state.turn.root.classList.remove("is-live");
    showToast("Connection lost — the run was interrupted", "error");
    finishTurn();
  }
});

/**
 * A handshake can fail two ways, and they need opposite responses.
 *
 * The backend refuses a connection whose token is expired or missing, and
 * reconnecting with the same one would only be refused again — so the token is
 * refreshed first and the retry carries the new one. Anything else is the
 * analyzer being unreachable, which retrying does fix.
 */
socket.on("connect_error", async (error) => {
  const rejected = /sign|token|session|expire/i.test(error?.message || "");

  if (rejected && Auth.isSignedIn) {
    try {
      await Auth.refresh();
      socket.connect();  // the auth callback picks up the new token
      return;
    } catch {
      // The refresh token is dead too; Auth drops the session and the gate
      // comes back up, which is the only thing left that helps.
    }
  }

  if (!offline) showToast("Can't reach the analyzer — retrying…", "error");
  offline = true;
});

// ── Run events ───────────────────────────────────────────────────────────

/**
 * The run an incoming event is allowed to write into, or null.
 *
 * A dossier is a closed box: an event may only reach the run on screen if the
 * server stamped it with the dossier we are in *and* that run was opened in
 * the same dossier. Anything else belongs to a dossier that has since been
 * closed — a run cut short by a dropped connection, say, whose events arrive
 * after the analyst has moved on — and is dropped rather than written into
 * whatever happens to be on screen now.
 */
function liveTurn(data) {
  const turn = state.turn;
  if (!turn || turn.sessionId !== state.active.id) return null;
  if (data?.session_id && data.session_id !== state.active.id) return null;
  return turn;
}

socket.on("run_started", (data) => {
  const turn = liveTurn(data);
  if (turn) turn.runId = data.run_id;
});

// Progress in the analyst's terms, not the graph's.
socket.on("status", (data) => {
  const turn = liveTurn(data);
  if (!turn) return;

  if (data.stage === "retrieve") setWork(turn, "reading the filing");
  if (data.stage === "route") setWork(turn, "working out what you're asking");
  if (data.stage === "analyze") {
    setWork(turn, `writing the ${labelOf(data.category).toLowerCase()} answer`);
  }
});

socket.on("route", (data) => {
  const turn = liveTurn(data);
  if (turn) tagRun(turn, data.category);
});

// A dossier is named once, after the question that opened it — every later
// question carries that name back to the analyzer, which leaves it alone.
socket.on("title", (data) => nameDossier(data));

/**
 * Record the name the analyzer gave a dossier.
 *
 * Keyed off the id the event carries rather than the dossier on screen: the
 * name belongs to the dossier the run was opened in, whichever one the analyst
 * happens to be looking at when it lands.
 */
function nameDossier(data) {
  const dossier = dossierById(data?.session_id);
  if (!dossier || !data.title || dossier.title === data.title) return;

  dossier.title = data.title;
  renderDossiers();
  if (dossier === state.active) stampSession();
}

socket.on("token", (data) => {
  const turn = liveTurn(data);
  if (!turn || !data.content) return;
  clearWork(turn);
  turn.raw += data.content;
  turn.body.hidden = false;
  turn.body.innerHTML =
    typeof marked !== "undefined" ? marked.parse(turn.raw) : escapeHtml(turn.raw);
  turn.body.classList.add("streaming");
  scrollToBottom();
});

// `done` and `error` close the run out, so a stale one must return rather than
// fall through to finishTurn() — that would end whichever run is live now.
socket.on("done", (data) => {
  // Ahead of the staleness check: a name is worth keeping even when it belongs
  // to a dossier the analyst has already moved on from.
  nameDossier(data);

  const turn = liveTurn(data);
  if (!turn) return;

  clearWork(turn);
  turn.body.classList.remove("streaming");
  if (!turn.raw.trim()) {
    turn.body.hidden = false;
    turn.body.innerHTML = "<p><em>No answer came back for this question.</em></p>";
  }
  // Belt and braces: the run carries its tag even if `route` was missed.
  tagRun(turn, data.category);
  turn.root.classList.remove("is-live");
  finishTurn();
});

socket.on("error", (data) => {
  const turn = liveTurn(data);
  if (!turn) return;

  clearWork(turn);
  turn.body.classList.remove("streaming");
  turn.root.classList.remove("is-live");
  addFault(turn, data.message || "Run failed");

  showToast(data.message || "Run failed", "error");
  finishTurn();
});

// ── Stored ledger ────────────────────────────────────────────────────────
/**
 * The workbench is a view of what the backend has stored, not a thing that
 * exists only while the tab is open. Dossiers, their runs and the filings they
 * read are all kept server-side, so signing in anywhere picks up where the
 * last session left off.
 *
 * A dossier's runs are fetched the first time it is opened rather than up
 * front: an analyst with forty dossiers should not wait for thirty-nine they
 * are not going to look at.
 */
const PAGE_LIMIT = 50;

async function loadDossiers() {
  const res = await Auth.authFetch("/api/conversations");
  if (!res.ok) throw new Error(`Could not list your dossiers (${res.status})`);

  const rows = await res.json();
  return rows.map((row) => {
    const dossier = makeDossier();
    dossier.id = row.id;
    dossier.title = row.title || "";
    dossier.indexed = (row.filings || []).map((filing) => ({ name: filing.name }));
    // A run is a question and its answer, so the tally is close enough to show
    // in the dock before the runs themselves are fetched; opening the dossier
    // replaces it with the count the ledger actually recorded.
    dossier.runNo = Math.ceil((row.message_count || 0) / 2);
    // An empty dossier has nothing to fetch, so it counts as already loaded.
    dossier.loaded = !row.message_count;
    return dossier;
  });
}

/**
 * Fill a dossier's stack from the ledger, newest page first.
 *
 * Failing here is not fatal: the dossier opens empty with a note saying so,
 * and asking a question still works — the run is appended to whatever the
 * backend already has, which is the record that matters.
 */
async function hydrateDossier(dossier, before = null) {
  if (dossier.loading) return;
  dossier.loading = true;
  renderDossiers();

  try {
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (before !== null) params.set("before_seq", String(before));

    const res = await Auth.authFetch(
      `/api/conversations/${encodeURIComponent(dossier.id)}/messages?${params}`
    );
    if (res.status === 404) {
      // Discarded from another tab. Nothing to restore, and nothing wrong.
      dossier.loaded = true;
      return;
    }
    if (!res.ok) throw new Error(`Could not read this dossier (${res.status})`);

    const page = await res.json();
    dossier.loaded = true;
    dossier.earlier = page.next_before_seq ?? null;
    renderStoredRuns(dossier, page.messages || [], before !== null);
  } catch (err) {
    dossier.loaded = true;  // do not retry on every open
    showToast(
      err instanceof TypeError
        ? "Can't reach the analyzer — this dossier's history is not shown"
        : err.message,
      "error"
    );
  } finally {
    dossier.loading = false;
    if (dossier === state.active) {
      messagesList.replaceChildren(dossier.stack);
      if (welcomeHero) welcomeHero.hidden = dossier.stack.childElementCount > 0;
      if (before === null) scrollToBottom(true);
    }
    renderDossiers();
  }
}

/**
 * Draw a page of stored messages as runs.
 *
 * Pages arrive aligned to whole runs — the backend leaves an answer with the
 * page its question is on — so pairing is a straight walk: a question opens a
 * run, the answer that follows closes it.
 */
function renderStoredRuns(dossier, messages, prepend) {
  const fragment = document.createDocumentFragment();
  let current = null;

  messages.forEach((message) => {
    if (message.role === "user") {
      current = storedRun(dossier, message);
      fragment.appendChild(current.root);
      return;
    }
    if (!current) {
      // An answer with no question in front of it: only reachable if a run was
      // recorded oddly. Shown on its own rather than dropped.
      current = storedRun(dossier, null);
      fragment.appendChild(current.root);
    }
    fillStoredAnswer(current, message);
    current = null;
  });

  if (prepend) dossier.stack.prepend(fragment);
  else dossier.stack.appendChild(fragment);

  // The tally is the highest run number on the stack, so the next question
  // carries on the numbering rather than restarting it.
  const runs = messages.filter((m) => m.role === "user");
  const highest = runs.reduce((n, m) => Math.max(n, m.meta?.run || 0), 0);
  dossier.runNo = prepend
    ? Math.max(dossier.runNo, highest)
    : Math.max(highest, runs.length);

  renderEarlierControl(dossier);
}

/** The "load earlier" control at the top of a dossier that has more behind it. */
function renderEarlierControl(dossier) {
  const existing = dossier.stack.querySelector(".load-earlier");
  if (existing) existing.remove();
  if (dossier.earlier === null) return;

  const button = document.createElement("button");
  button.className = "load-earlier";
  button.textContent = "load earlier runs";
  button.addEventListener("click", () => {
    button.disabled = true;
    button.textContent = "loading…";
    hydrateDossier(dossier, dossier.earlier);
  });
  dossier.stack.prepend(button);
}

/** One finished run, rebuilt from the ledger. */
function storedRun(dossier, ask) {
  const files = ask?.meta?.files || [];
  const article = document.createElement("article");
  article.className = "run";
  article.innerHTML = `
    <div class="run-bar">
      <span class="run-no">RUN ${String(ask?.meta?.run || dossier.runNo + 1).padStart(2, "0")}</span>
      <span class="run-badge" hidden></span>
      <span class="run-time">${escapeHtml(timeOf(ask?.created_at))}</span>
    </div>

    <div class="run-ask">
      <span class="ask-caret">▸</span>
      <div class="ask-body">
        ${
          files.length
            ? `<div class="ask-files">${files
                .map(
                  (name) => `<span class="ask-file" data-file="${escapeHtml(name)}">
                            <span class="af-kind">${fileKind(name)}</span>
                            <span class="af-name">${escapeHtml(name)}</span>
                            <span class="af-state">ready</span>
                          </span>`
                )
                .join("")}</div>`
            : ""
        }
        ${
          ask
            ? `<p class="ask-text">${escapeHtml(ask.content)}</p>`
            : '<p class="ask-text muted">— question not recorded —</p>'
        }
      </div>
    </div>

    <div class="out">
      <div class="markdown-body" hidden></div>
    </div>
  `;

  article.querySelectorAll(".ask-file").forEach((el) => el.classList.add("indexed"));

  return {
    root: article,
    out: article.querySelector(".out"),
    badge: article.querySelector(".run-badge"),
    body: article.querySelector(".markdown-body"),
  };
}

/** Put a stored answer — or the fault that replaced it — into its run. */
function fillStoredAnswer(run, message) {
  if (message.status === "error") {
    addFault(run, message.content || "Run failed");
    return;
  }

  run.body.hidden = false;
  run.body.innerHTML =
    typeof marked !== "undefined"
      ? marked.parse(message.content || "")
      : escapeHtml(message.content || "");
  if (message.meta?.category) tagRun(run, message.meta.category);
}

/** A stored timestamp as the clock time the run bar shows. */
function timeOf(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? ""
    : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ── Dispatch ─────────────────────────────────────────────────────────────
async function submitQuery(overrideText) {
  if (state.busy) return;

  // The gate is up, or the session lapsed between opening the page and asking
  // — either way there is no account to read filings for.
  if (!Auth.isSignedIn) {
    showToast("Sign in to run a query", "error");
    return;
  }

  const dossier = state.active;
  const text = (overrideText !== undefined ? overrideText : userInput.value).trim();
  const files = [...dossier.pending];

  if (!text && files.length === 0) {
    userInput.focus();
    return;
  }

  if (welcomeHero) welcomeHero.hidden = true;
  setBusy(true);

  const turn = createRun({ text, files, dossier });
  state.turn = turn;

  if (overrideText === undefined) {
    userInput.value = "";
    autoGrow();
  }

  // Ingest the staged filings first so this same run can read from them.
  // Everything below is keyed off `turn.sessionId`, not the live one: a run
  // uploads into, and asks of, the dossier it was opened in — never another.
  if (files.length) {
    setWork(turn, "adding the filing");
    const ok = await uploadPending(files, turn);
    if (!ok) {
      clearWork(turn);
      turn.root.classList.remove("is-live");
      finishTurn();
      return;
    }
  }

  // The name goes out with the question: sending the one this dossier already
  // has is what stops the analyzer renaming it on every run.
  socket.emit("query", {
    query: text,
    session_id: turn.sessionId,
    title: dossier.title,
    // Recorded with the question, so a reopened dossier still shows which
    // filings a run was asked against.
    files: files.map((f) => f.name),
  });
  scrollToBottom(true);
}

function finishTurn() {
  state.turn = null;
  setBusy(false);
}

function setBusy(busy) {
  state.busy = busy;
  sendBtn.disabled = busy;
  userInput.disabled = busy;
  attachBtn.disabled = busy;
  document.body.classList.toggle("is-busy", busy);
  if (!busy) userInput.focus();
}

// ── Staged filings ───────────────────────────────────────────────────────
function stageFiles(fileList) {
  // A filing rides along with a question, so there is nowhere to put one while
  // a run is already on the wire.
  if (state.busy) {
    showToast("Wait for the run to finish before attaching a filing", "info");
    return;
  }

  const files = [...fileList].filter((f) => {
    const ok = ACCEPTED_EXT.some((ext) => f.name.toLowerCase().endsWith(ext));
    if (!ok) showToast(`Unsupported file type: ${f.name}`, "error");
    return ok;
  });
  if (!files.length) return;

  files.forEach((file) => {
    state.active.pending.push({ id: newId(), file, name: file.name, size: file.size, status: "staged" });
  });
  renderTray();
  userInput.focus();
}

function renderTray() {
  const pending = state.active.pending;
  attachTray.innerHTML = "";
  attachTray.hidden = pending.length === 0;

  pending.forEach((item) => {
    const chip = document.createElement("div");
    chip.className = `tray-chip is-${item.status}`;
    chip.innerHTML = `
      <span class="tc-kind">${fileKind(item.name)}</span>
      <span class="tc-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
      <span class="tc-state">${
        item.status === "uploading"
          ? "adding"
          : item.status === "done"
          ? "ready"
          : item.status === "error"
          ? "failed"
          : formatBytes(item.size)
      }</span>
      ${
        item.status === "uploading"
          ? '<span class="tc-spin"></span>'
          : `<button class="tc-drop" data-id="${item.id}" aria-label="Remove ${escapeHtml(item.name)}">
               <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round">
                 <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
               </svg>
             </button>`
      }
    `;
    attachTray.appendChild(chip);
  });

  attachTray.querySelectorAll(".tc-drop").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.active.pending = state.active.pending.filter((f) => f.id !== btn.dataset.id);
      renderTray();
    });
  });
}

async function uploadPending(files, turn) {
  let allOk = true;
  const dossier = (turn && dossierById(turn.sessionId)) || state.active;
  const sessionId = dossier.id;

  for (const item of files) {
    item.status = "uploading";
    if (dossier === state.active) renderTray();

    const formData = new FormData();
    formData.append("file", item.file);
    formData.append("session_id", sessionId);

    try {
      // authFetch attaches the access token and, if it has just expired,
      // refreshes and replays this upload rather than losing the filing.
      const res = await Auth.authFetch("/api/upload", { method: "POST", body: formData });

      // A failure can come back as something other than JSON (a proxy's error
      // page, say), and parsing that would replace the real reason with a
      // parser error — so the body is read defensively.
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          data.detail || `The analyzer rejected the file (${res.status} ${res.statusText})`
        );
      }

      item.status = "done";
      // The filing joins the register of the dossier it was uploaded into,
      // which is only the one on screen if the analyst has not moved on.
      dossier.indexed.push({ name: data.filename || item.name });
      if (dossier === state.active) renderFilings();
      if (turn) markRunFile(turn, item.name, "ready");
    } catch (err) {
      item.status = "error";
      allOk = false;
      if (turn) markRunFile(turn, item.name, "failed", true);
      // fetch rejects with a TypeError when the request never reached the
      // backend — a reason worth stating, since it is not the file's fault.
      const reason =
        err instanceof TypeError
          ? "the analyzer is not reachable — check the backend is running"
          : err.message;
      // The backend names the file in most of its reasons; only say it again
      // when it doesn't, so the analyst reads one sentence rather than two.
      const detail = reason.includes(item.name)
        ? reason
        : `Could not add "${item.name}" — ${reason}`;

      if (turn) addFault(turn, detail);
      showToast(detail, "error");
    }
  }

  if (allOk) dossier.pending = [];
  if (dossier === state.active) renderTray();
  return allOk;
}

function renderFilings() {
  const indexed = state.active.indexed;
  fileCountBadge.textContent = String(indexed.length).padStart(2, "0");

  uploadedFilesList.innerHTML = "";
  if (!indexed.length) {
    uploadedFilesList.innerHTML = '<p class="list-empty">nothing in this dossier yet</p>';
    return;
  }

  indexed.forEach((file, i) => {
    const row = document.createElement("div");
    row.className = "filing-row";
    row.innerHTML = `
      <span class="fr-no">${String(i + 1).padStart(2, "0")}</span>
      <span class="fr-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
    `;
    uploadedFilesList.appendChild(row);
  });
}

// ── Run records ──────────────────────────────────────────────────────────
function createRun({ text, files, dossier }) {
  dossier.runNo += 1;

  const article = document.createElement("article");
  article.className = "run is-live";
  article.innerHTML = `
    <div class="run-bar">
      <span class="run-no">RUN ${String(dossier.runNo).padStart(2, "0")}</span>
      <span class="run-badge" hidden></span>
      <span class="run-time">${timeNow()}</span>
    </div>

    <div class="run-ask">
      <span class="ask-caret">▸</span>
      <div class="ask-body">
        ${
          files.length
            ? `<div class="ask-files">${files
                .map(
                  (f) => `<span class="ask-file" data-file="${escapeHtml(f.name)}">
                            <span class="af-kind">${fileKind(f.name)}</span>
                            <span class="af-name">${escapeHtml(f.name)}</span>
                            <span class="af-state">staging</span>
                          </span>`
                )
                .join("")}</div>`
            : ""
        }
        ${text ? `<p class="ask-text">${escapeHtml(text)}</p>` : '<p class="ask-text muted">— overview of the attached filing —</p>'}
      </div>
    </div>

    <div class="out">
      <div class="work" hidden><span class="work-bar"></span><span class="work-text"></span></div>
      <div class="markdown-body" hidden></div>
    </div>
  `;

  dossier.stack.appendChild(article);

  const turn = {
    root: article,
    out: article.querySelector(".out"),
    badge: article.querySelector(".run-badge"),
    work: article.querySelector(".work"),
    workText: article.querySelector(".work-text"),
    body: article.querySelector(".markdown-body"),
    raw: "",
    runId: null,
    sessionId: dossier.id,  // the dossier this run belongs to, for its life
    category: null,         // the kind of analysis, once it is known
  };

  renderDossiers();  // its dossier's run tally just moved
  setWork(turn, "starting");
  scrollToBottom(true);
  return turn;
}

function markRunFile(turn, name, label, failed = false) {
  const el = [...turn.root.querySelectorAll(".ask-file")].find(
    (n) => n.dataset.file === name
  );
  if (!el) return;
  el.classList.add(failed ? "failed" : "indexed");
  el.querySelector(".af-state").textContent = label;
}

function setWork(turn, text) {
  turn.work.hidden = false;
  turn.workText.textContent = text;
}

/**
 * Record why a run went wrong on the run itself. A toast says it once and
 * leaves; the ledger has to still explain the failure when the analyst looks
 * back at it, so anything that stops a run is written here too.
 */
function addFault(turn, message) {
  const fault = document.createElement("div");
  fault.className = "fault";
  fault.textContent = message;
  turn.out.appendChild(fault);
  scrollToBottom();
}

function clearWork(turn) {
  turn.work.hidden = true;
}

/**
 * Tag the run with the kind of analysis behind its answer — the one piece of
 * routing worth surfacing, since it tells the reader what they are looking at.
 * Set on the `route` event and again on `done`, so an answer is never untagged.
 */
function tagRun(turn, category) {
  const meta = CATEGORY_BY_ID[category];
  if (!meta) return;
  turn.category = category;
  turn.badge.hidden = false;
  turn.badge.dataset.tone = meta.tone;
  turn.badge.textContent = meta.label;
}

// ── Wiring ───────────────────────────────────────────────────────────────
sendBtn.addEventListener("click", () => submitQuery());

userInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitQuery();
  }
});

userInput.addEventListener("input", autoGrow);

userInput.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    stageFiles(files);
  }
});

function autoGrow() {
  userInput.style.height = "auto";
  userInput.style.height = `${Math.min(userInput.scrollHeight, 190)}px`;
}

// Opening cards are just pre-written questions.
document.querySelectorAll(".op-card").forEach((btn) => {
  btn.addEventListener("click", () => submitQuery(btn.dataset.query));
});

clearChatBtn.addEventListener("click", async () => {
  if (state.busy) return;
  await discardDossier(state.active);
  showToast("Dossier discarded", "info");
});

newChatBtn.addEventListener("click", () => {
  if (state.busy) return;
  // The dossiers already open stay open, filings and all — a new one simply
  // starts empty alongside them.
  newDossier();
  closeDockOnMobile();
  showToast("New dossier opened", "success");
});

/**
 * Put a dossier on the stage: its ledger, its filings, its name.
 *
 * The ledger entries are moved rather than rebuilt — each dossier keeps its
 * own stack of them — so a dossier comes back exactly as it was left, down to
 * the passages and faults recorded on each run.
 */
function openDossier(dossier) {
  state.active = dossier;
  // Any run still on the wire belongs to the dossier being left; `liveTurn`
  // will now drop whatever it sends back.
  state.turn = null;

  messagesList.replaceChildren(dossier.stack);
  if (welcomeHero) welcomeHero.hidden = dossier.stack.childElementCount > 0;

  renderTray();
  renderFilings();
  renderDossiers();
  stampSession();
  scrollToBottom(true);

  // A dossier restored from the ledger has its name and its filings but not
  // its runs; they are fetched now, on the first open, and the stack is
  // redrawn when they land.
  if (!dossier.loaded) hydrateDossier(dossier);
}

/** Open a new, empty dossier. Whatever else is open is left as it is. */
function newDossier() {
  const dossier = makeDossier();
  state.dossiers.push(dossier);
  openDossier(dossier);
  return dossier;
}

/**
 * Discard a dossier and everything it read: its runs, and the filings they
 * were drawn from. Both go on the backend, so a discarded dossier does not
 * come back on the next sign-in.
 *
 * The workbench always has one dossier open, so discarding the last one opens
 * a fresh dossier in its place rather than leaving an empty stage.
 */
async function discardDossier(dossier) {
  await deleteDossier(dossier.id);

  const at = state.dossiers.indexOf(dossier);
  state.dossiers.splice(at, 1);

  const next = state.dossiers[at] || state.dossiers[at - 1];
  if (next) openDossier(next);
  else newDossier();
}

/**
 * The dock's register of open dossiers, each under the name the analyzer gave
 * it. One click puts a dossier back on the stage.
 */
function renderDossiers() {
  dossierCountBadge.textContent = String(state.dossiers.length).padStart(2, "0");
  dossierList.innerHTML = "";

  state.dossiers.forEach((dossier) => {
    const name = dossierName(dossier);
    const row = document.createElement("button");
    row.className = "dossier-row";
    row.classList.toggle("is-active", dossier === state.active);
    row.classList.toggle("is-unnamed", !dossier.title);
    row.classList.toggle("is-loading", dossier.loading);
    row.title = name;
    row.innerHTML = `
      <span class="dr-name">${escapeHtml(name)}</span>
      <span class="dr-tally">${String(dossier.runNo).padStart(2, "0")}</span>
    `;

    row.addEventListener("click", () => {
      if (dossier === state.active) return;
      // A run reads and answers within one dossier; switching mid-run would
      // leave it writing into a ledger that is no longer on screen.
      if (state.busy) {
        showToast("Wait for the run to finish before switching dossier", "info");
        return;
      }
      openDossier(dossier);
      closeDockOnMobile();
    });

    dossierList.appendChild(row);
  });
}

/** A dossier goes by its id until the analyzer names it. */
function dossierName(dossier) {
  return dossier.title || `DOSSIER ${dossier.id.slice(0, 6).toUpperCase()}`;
}

/**
 * Delete a dossier on the backend — its messages, and the collection its
 * filings live in. Each dossier has its own collection, so nothing it read can
 * leak into the next one.
 */
async function deleteDossier(sessionId) {
  try {
    await Auth.authFetch(`/api/conversations/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
  } catch {
    // The dossier is gone from the workbench either way. A record left behind
    // on the backend reappears on the next sign-in rather than being lost,
    // which is the better way round to fail.
  }
}

attachBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", (e) => {
  if (e.target.files?.length) stageFiles(e.target.files);
  fileInput.value = "";
});

let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (state.busy) return;
  if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
  dragDepth++;
  dragOverlay.classList.add("active");
});
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dragOverlay.classList.remove("active");
});
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  dragOverlay.classList.remove("active");
  if (e.dataTransfer?.files?.length) stageFiles(e.dataTransfer.files);
});

dockToggle.addEventListener("click", () => dock.classList.toggle("open"));
dockClose.addEventListener("click", closeDockOnMobile);
scrim.addEventListener("click", closeDockOnMobile);

function closeDockOnMobile() {
  dock.classList.remove("open");
}

// ── Utilities ────────────────────────────────────────────────────────────

/**
 * The ledger follows the stream only while the analyst is parked at the
 * bottom. Scrolling up mid-run detaches it — tokens keep arriving without
 * yanking the view back — and scrolling to the end re-arms it.
 */
let stickToBottom = true;

chatContainer.addEventListener(
  "scroll",
  () => {
    const gap =
      chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
    stickToBottom = gap < 90;
  },
  { passive: true }
);

function scrollToBottom(force = false) {
  if (force) stickToBottom = true;
  if (!stickToBottom) return;
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function labelOf(category) {
  return CATEGORY_BY_ID[category]?.label || category || "analysis";
}

function fileKind(name) {
  return (name.split(".").pop() || "doc").toLowerCase();
}

function formatBytes(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)}${units[i]}`;
}

function timeNow() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function newId() {
  return (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`).replace(/-/g, "");
}

function stampSession() {
  const dossier = state.active;
  sessionIdEl.textContent = `DOSSIER ${dossier.id.slice(0, 6).toUpperCase()}`;
  dossierTitleEl.textContent = dossier.title;
  dossierTitleEl.hidden = !dossier.title;
}

function escapeHtml(text) {
  if (text === undefined || text === null) return "";
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 300);
  }, 4200);
}

// ── Session ──────────────────────────────────────────────────────────────
/**
 * Open the workbench for a signed-in analyst.
 *
 * Their dossiers come back with them: the backend keeps the runs, the names
 * and the filings, all scoped to the account, so signing in resumes the work
 * rather than starting it over. The most recent dossier goes on the stage and
 * fills itself in; an analyst signing in for the first time gets an empty one.
 *
 * The connection is opened first — it does not depend on the ledger, and a
 * dossier that cannot be listed should still be able to answer a question.
 */
async function startSession(user) {
  stampUser(user);
  state.dossiers = [];
  state.turn = null;
  socket.connect();

  let restored = [];
  try {
    restored = await loadDossiers();
  } catch (err) {
    showToast(
      err instanceof TypeError
        ? "Can't reach the analyzer — starting with an empty dossier"
        : err.message,
      "error"
    );
  }

  // Guard against a sign-out, or a second sign-in, landing while the list was
  // in flight: whatever happened last is what should be on the stage.
  if (!Auth.isSignedIn) return;

  state.dossiers = restored;
  if (state.dossiers.length) openDossier(state.dossiers[0]);
  else newDossier();

  userInput.focus();
}

/**
 * Tear the workbench down on the way out.
 *
 * The whole stage is cleared, not just the connection, so the next analyst to
 * sign in on this browser is not handed the last one's questions on screen.
 */
function endSession() {
  socket.disconnect();

  state.dossiers = [];
  state.turn = null;
  setBusy(false);
  messagesList.replaceChildren();
  if (welcomeHero) welcomeHero.hidden = false;
  stampUser(null);
  newDossier();
}

/** Put the signed-in analyst's name on the dock. */
function stampUser(user) {
  userName.textContent = user?.name || "—";
  userEmail.textContent = user?.email || "";
  userInitial.textContent = (user?.name || "?").trim().charAt(0).toUpperCase();
}

window.addEventListener("auth:signedin", (event) => startSession(event.detail?.user));

window.addEventListener("auth:signedout", (event) => {
  endSession();
  if (event.detail?.message) showToast(event.detail.message, "error");
});

logoutBtn.addEventListener("click", () => {
  if (state.busy) {
    showToast("Wait for the run to finish before signing out", "info");
    return;
  }
  Auth.logout();
});

// ── Init ─────────────────────────────────────────────────────────────────
newDossier();  // renders the dock, the register and the stamp with it
autoGrow();
