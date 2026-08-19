/**
 * Corporate Filing Analyzer — Analyst Workbench client
 *
 * Talks to the FastAPI + LangGraph backend over Socket.IO. Each question is a
 * numbered run in the ledger, and each run traces the graph it walked:
 *
 *   retrieve → router → approval (interrupt) → <category> node
 *
 * Choosing a node in the command bar skips the router; leaving it on `auto`
 * lets the LLM classify and — with the approval gate armed — holds the run so
 * the analyst can confirm or redirect it before analysis starts.
 */

// ── Configuration ────────────────────────────────────────────────────────
const BACKEND_URL =
  window.location.port === "8000"
    ? window.location.origin
    : "http://localhost:8000";

const CATEGORIES = [
  { id: "financials",   label: "Financials",   tone: "jade",   desc: "revenue · margins · EBITDA · balance sheet" },
  { id: "compliance",   label: "Compliance",   tone: "blue",   desc: "auditor opinion · SOX 404 · legal" },
  { id: "risks",        label: "Risks",        tone: "rose",   desc: "item 1A · market · cyber · credit" },
  { id: "shareholding", label: "Shareholding", tone: "violet", desc: "ownership · institutional holders" },
  { id: "governance",   label: "Governance",   tone: "cyan",   desc: "board · committees · compensation" },
  { id: "mda",          label: "MD&A",         tone: "gold",   desc: "outlook · strategy · segments" },
  { id: "summary",      label: "Summary",      tone: "amber",  desc: "full-filing executive briefing" },
  { id: "qa",           label: "Q&A",          tone: "slate",  desc: "open questions on the filing" },
];

const CATEGORY_BY_ID = Object.fromEntries(CATEGORIES.map((c) => [c.id, c]));
const ACCEPTED_EXT = [".pdf", ".txt", ".md", ".csv"];

if (typeof marked !== "undefined") {
  marked.setOptions({ breaks: true, gfm: true });
}

// ── State ────────────────────────────────────────────────────────────────
const state = {
  sessionId: newId(),
  pending: [],        // filings staged in the command bar
  indexed: [],        // filings ingested into the vector store this session
  category: "auto",   // "auto" → router node, else a direct entry point
  requireApproval: true,
  busy: false,
  runNo: 0,
  turn: null,         // the run currently on the wire
};

// ── DOM ──────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const chatContainer = $("chatContainer");
const messagesList = $("messagesList");
const welcomeHero = $("welcomeHero");
const userInput = $("userInput");
const sendBtn = $("sendBtn");
const attachBtn = $("attachBtn");
const attachTray = $("attachTray");
const dropzone = $("dropzone");
const fileInput = $("fileInput");
const uploadedFilesList = $("uploadedFilesList");
const fileCountBadge = $("fileCountBadge");
const clearChatBtn = $("clearChatBtn");
const newChatBtn = $("newChatBtn");
const dock = $("dock");
const dockToggle = $("dockToggle");
const dockClose = $("dockClose");
const scrim = $("scrim");
const toastContainer = $("toastContainer");
const serverPulseDot = $("serverPulseDot");
const serverStatusText = $("serverStatusText");
const modelTag = $("modelTag");
const approvalToggle = $("approvalToggle");
const approvalSwitch = $("approvalSwitch");
const sessionSub = $("sessionSub");
const sessionIdEl = $("sessionId");
const routePickerBtn = $("routePickerBtn");
const routePickerLabel = $("routePickerLabel");
const routeMenu = $("routeMenu");
const dragOverlay = $("dragOverlay");
const inspector = $("inspector");
const inspectorToggle = $("inspectorToggle");
const graphMap = $("graphMap");
const graphState = $("graphState");
const inspSources = $("inspSources");
const sourceCount = $("sourceCount");
const statRuns = $("statRuns");
const statFilings = $("statFilings");
const statChunks = $("statChunks");

// ── Socket.IO ────────────────────────────────────────────────────────────
const socket = io(BACKEND_URL, {
  transports: ["websocket", "polling"],
  reconnectionAttempts: 20,
  reconnectionDelay: 1500,
});

socket.on("connect", () => {
  serverPulseDot.classList.add("online");
  serverStatusText.textContent = "linked";
  checkBackendHealth();
});

socket.on("disconnect", () => {
  serverPulseDot.classList.remove("online");
  serverStatusText.textContent = "reconnecting…";
  if (state.busy) {
    if (state.turn) state.turn.root.classList.remove("is-live");
    showToast("Connection lost — the run was interrupted", "error");
    finishTurn();
  }
});

socket.on("connect_error", () => {
  serverPulseDot.classList.remove("online");
  serverStatusText.textContent = "backend offline";
});

// ── Graph run events ─────────────────────────────────────────────────────
socket.on("run_started", (data) => {
  if (state.turn) state.turn.runId = data.run_id;
});

socket.on("status", (data) => {
  const turn = state.turn;
  if (!turn) return;

  if (data.stage === "retrieve") {
    setStep(turn, "retrieve", "active");
    setNode("retrieve", "active");
    setWork(turn, "pulling passages from the index");
  }
  if (data.stage === "route") {
    setStep(turn, "retrieve", "done");
    setNode("retrieve", "done");
    setStep(turn, "route", "active");
    setNode("router", "active");
    setWork(turn, "classifying the question");
  }
  if (data.stage === "analyze") {
    ["retrieve", "route", "approve"].forEach((s) => setStep(turn, s, "done"));
    setNode("retrieve", "done");
    setNode("router", turn.routeSource === "manual" ? "skipped" : "done");
    setStep(turn, "analyze", "active");
    setNode(data.category, "active");
    setGraphState("analyzing");
    setWork(turn, `running ${labelOf(data.category).toLowerCase()} analysis`);
  }
});

socket.on("sources", (data) => {
  if (state.turn) renderSources(state.turn, data.sources || []);
});

socket.on("route", (data) => {
  const turn = state.turn;
  if (!turn) return;
  setBadge(turn, data.category, data.source);
  if (data.source !== "manual") {
    setStep(turn, "route", "done");
    setNode("router", "done");
  }
  if (data.source === "human") {
    setStep(turn, "approve", "done");
    setNode("approval", "done");
  }
  highlightLeaf(data.category);
});

socket.on("interrupt", (data) => {
  const turn = state.turn;
  if (!turn) return;
  turn.runId = data.run_id;
  setStep(turn, "route", "done");
  setNode("router", "done");
  setStep(turn, "approve", "paused");
  setNode("approval", "paused");
  setGraphState("held");
  clearWork(turn);
  renderApprovalCard(turn, data);
  scrollToBottom(true);
});

socket.on("resumed", () => {
  const turn = state.turn;
  if (!turn) return;
  setStep(turn, "approve", "done");
  setNode("approval", "done");
  setGraphState("resumed");
  setWork(turn, "resuming from the gate");
});

socket.on("token", (data) => {
  const turn = state.turn;
  if (!turn || !data.content) return;
  clearWork(turn);
  turn.raw += data.content;
  turn.body.hidden = false;
  turn.body.innerHTML =
    typeof marked !== "undefined" ? marked.parse(turn.raw) : escapeHtml(turn.raw);
  turn.body.classList.add("streaming");
  scrollToBottom();
});

socket.on("done", (data) => {
  const turn = state.turn;
  if (turn) {
    setStep(turn, "analyze", "done");
    setNode(data.category, "done");
    clearWork(turn);
    turn.body.classList.remove("streaming");
    if (!turn.raw.trim()) {
      turn.body.hidden = false;
      turn.body.innerHTML = "<p><em>No content returned for this run.</em></p>";
    }
    setBadge(turn, data.category, turn.routeSource);
    turn.root.classList.remove("is-live");
  }
  setGraphState("idle");
  finishTurn();
});

socket.on("error", (data) => {
  const turn = state.turn;
  if (turn) {
    clearWork(turn);
    turn.body.classList.remove("streaming");
    turn.root.classList.remove("is-live");
    const err = document.createElement("div");
    err.className = "fault";
    err.textContent = data.message || "Run failed";
    turn.out.appendChild(err);
  }
  setGraphState("fault");
  showToast(data.message || "Run failed", "error");
  finishTurn();
});

// ── Dispatch ─────────────────────────────────────────────────────────────
async function submitQuery(overrideText, overrideCategory) {
  if (state.busy) return;

  const text = (overrideText !== undefined ? overrideText : userInput.value).trim();
  const category = overrideCategory || state.category;
  const files = [...state.pending];

  if (!text && files.length === 0) {
    userInput.focus();
    return;
  }

  if (welcomeHero) welcomeHero.hidden = true;
  setBusy(true);

  const turn = createRun({ text, files, category });
  state.turn = turn;

  if (overrideText === undefined) {
    userInput.value = "";
    autoGrow();
  }

  // Ingest the staged filings first so this same run can retrieve from them.
  if (files.length) {
    setWork(turn, "vectorising and indexing the filing");
    const ok = await uploadPending(files, turn);
    if (!ok) {
      setStep(turn, "retrieve", "cancelled");
      clearWork(turn);
      turn.root.classList.remove("is-live");
      finishTurn();
      return;
    }
  }

  const requireApproval = category === "auto" && state.requireApproval;
  socket.emit("query", {
    query: text,
    category,
    require_approval: requireApproval,
    session_id: state.sessionId,
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
  document.body.classList.toggle("is-busy", busy);
  if (!busy) userInput.focus();
}

// ── Staged filings ───────────────────────────────────────────────────────
function stageFiles(fileList) {
  const files = [...fileList].filter((f) => {
    const ok = ACCEPTED_EXT.some((ext) => f.name.toLowerCase().endsWith(ext));
    if (!ok) showToast(`Unsupported file type: ${f.name}`, "error");
    return ok;
  });
  if (!files.length) return;

  files.forEach((file) => {
    state.pending.push({ id: newId(), file, name: file.name, size: file.size, status: "staged" });
  });
  renderTray();
  userInput.focus();
}

function renderTray() {
  attachTray.innerHTML = "";
  attachTray.hidden = state.pending.length === 0;

  state.pending.forEach((item) => {
    const chip = document.createElement("div");
    chip.className = `tray-chip is-${item.status}`;
    chip.innerHTML = `
      <span class="tc-kind">${fileKind(item.name)}</span>
      <span class="tc-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
      <span class="tc-state">${
        item.status === "uploading"
          ? "indexing"
          : item.status === "done"
          ? `${item.chunks} chunks`
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
      state.pending = state.pending.filter((f) => f.id !== btn.dataset.id);
      renderTray();
    });
  });
}

async function uploadPending(files, turn) {
  let allOk = true;

  for (const item of files) {
    item.status = "uploading";
    renderTray();

    const formData = new FormData();
    formData.append("file", item.file);
    formData.append("session_id", state.sessionId);

    try {
      const res = await fetch(`${BACKEND_URL}/api/upload`, { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Indexing failed");

      item.status = "done";
      item.chunks = data.chunks_ingested;
      state.indexed.push({ name: data.filename, chunks: data.chunks_ingested });
      renderFilings();
      if (turn) markRunFile(turn, item.name, `${data.chunks_ingested} chunks`);
    } catch (err) {
      item.status = "error";
      allOk = false;
      if (turn) markRunFile(turn, item.name, "failed", true);
      showToast(`Could not index "${item.name}": ${err.message}`, "error");
    }
  }

  renderTray();
  if (allOk) {
    state.pending = [];
    renderTray();
  }
  return allOk;
}

function renderFilings() {
  fileCountBadge.textContent = String(state.indexed.length).padStart(2, "0");
  statFilings.textContent = state.indexed.length;
  statChunks.textContent = state.indexed.reduce((n, f) => n + (f.chunks || 0), 0);

  uploadedFilesList.innerHTML = "";
  if (!state.indexed.length) {
    uploadedFilesList.innerHTML = '<p class="list-empty">nothing indexed in this dossier</p>';
    return;
  }

  state.indexed.forEach((file, i) => {
    const row = document.createElement("div");
    row.className = "filing-row";
    row.innerHTML = `
      <span class="fr-no">${String(i + 1).padStart(2, "0")}</span>
      <span class="fr-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
      <span class="fr-chunks">${file.chunks}</span>
    `;
    uploadedFilesList.appendChild(row);
  });
}

// ── Run records ──────────────────────────────────────────────────────────
function createRun({ text, files, category }) {
  state.runNo += 1;
  statRuns.textContent = state.runNo;

  const steps = ["retrieve"];
  if (category === "auto") steps.push("route");
  if (category === "auto" && state.requireApproval) steps.push("approve");
  steps.push("analyze");

  const article = document.createElement("article");
  article.className = "run is-live";
  article.innerHTML = `
    <div class="run-bar">
      <span class="run-no">RUN ${String(state.runNo).padStart(2, "0")}</span>
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
        ${text ? `<p class="ask-text">${escapeHtml(text)}</p>` : '<p class="ask-text muted">— default brief for this node —</p>'}
      </div>
    </div>

    <div class="run-grid">
      <div class="trace"></div>
      <div class="out">
        <div class="work" hidden><span class="work-bar"></span><span class="work-text"></span></div>
        <div class="markdown-body" hidden></div>
        <div class="run-sources" hidden></div>
      </div>
    </div>
  `;

  messagesList.appendChild(article);

  const turn = {
    root: article,
    trace: article.querySelector(".trace"),
    out: article.querySelector(".out"),
    badge: article.querySelector(".run-badge"),
    work: article.querySelector(".work"),
    workText: article.querySelector(".work-text"),
    body: article.querySelector(".markdown-body"),
    sourcesEl: article.querySelector(".run-sources"),
    steps: {},
    raw: "",
    runId: null,
    routeSource: category === "auto" ? "auto" : "manual",
  };

  const labels = { retrieve: "retrieve", route: "router", approve: "approval", analyze: "analysis" };
  steps.forEach((name) => {
    const el = document.createElement("div");
    el.className = "tr-step";
    el.dataset.state = "pending";
    el.innerHTML = `<i></i><span>${labels[name]}</span>`;
    turn.trace.appendChild(el);
    turn.steps[name] = el;
  });

  resetGraph(category);
  if (category !== "auto") {
    setBadge(turn, category, "manual");
    highlightLeaf(category);
  }
  setGraphState("running");
  setWork(turn, "opening the run");
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

function setStep(turn, name, status) {
  const el = turn.steps[name];
  if (!el) return;
  if (el.dataset.state === "done" && status === "active") return;
  el.dataset.state = status;
}

function setWork(turn, text) {
  turn.work.hidden = false;
  turn.workText.textContent = text;
}

function clearWork(turn) {
  turn.work.hidden = true;
}

function setBadge(turn, category, source) {
  const meta = CATEGORY_BY_ID[category];
  if (!meta) return;
  turn.routeSource = source || turn.routeSource;
  const how =
    turn.routeSource === "manual" ? "selected" : turn.routeSource === "human" ? "approved" : "auto";
  turn.badge.hidden = false;
  turn.badge.dataset.tone = meta.tone;
  turn.badge.innerHTML = `${escapeHtml(meta.label)}<em>${how}</em>`;
}

function renderSources(turn, sources) {
  if (!sources.length) return;

  turn.sourcesEl.hidden = false;
  turn.sourcesEl.innerHTML = `
    <button class="src-toggle">${sources.length} passage${sources.length === 1 ? "" : "s"} retrieved</button>
    <div class="src-list" hidden>
      ${sources.map(sourceRow).join("")}
    </div>
  `;
  const toggle = turn.sourcesEl.querySelector(".src-toggle");
  const list = turn.sourcesEl.querySelector(".src-list");
  toggle.addEventListener("click", () => {
    list.hidden = !list.hidden;
    toggle.classList.toggle("open", !list.hidden);
  });

  // Mirror into the inspector for the live run.
  sourceCount.textContent = String(sources.length).padStart(2, "0");
  inspSources.innerHTML = sources.map(sourceRow).join("");
}

function sourceRow(s) {
  return `<div class="src-item">
            <span class="src-ref">${escapeHtml(s.source)}${s.page ? ` · p.${s.page}` : ""}</span>
            <span class="src-text">${escapeHtml(s.snippet || "")}…</span>
          </div>`;
}

// ── Approval gate (human-in-the-loop) ────────────────────────────────────
function renderApprovalCard(turn, data) {
  const proposed = data.proposed_category;
  const meta = CATEGORY_BY_ID[proposed] || CATEGORY_BY_ID.qa;

  const card = document.createElement("div");
  card.className = "gatecard";
  card.dataset.tone = meta.tone;
  card.innerHTML = `
    <div class="gc-head">
      <span class="gc-flag"><i></i>held at approval</span>
      <span class="gc-node">→ ${escapeHtml(meta.label.toLowerCase())}</span>
    </div>
    <p class="gc-copy">
      The router read this as <strong>${escapeHtml(meta.label)}</strong>. Release it,
      or send the run down a different branch — it continues from the gate either way.
    </p>
    <div class="gc-actions">
      <button class="gc-go" data-approve>release → ${escapeHtml(meta.label.toLowerCase())}</button>
      <button class="gc-alt" data-change>redirect</button>
      <button class="gc-drop" data-cancel>abandon</button>
    </div>
    <div class="gc-branches" hidden>
      ${CATEGORIES.map(
        (c) => `<button class="gc-branch" data-category="${c.id}" data-tone="${c.tone}">
                  <span class="gb-name">${escapeHtml(c.label)}</span>
                  <span class="gb-desc">${escapeHtml(c.desc)}</span>
                </button>`
      ).join("")}
    </div>
  `;

  turn.out.insertBefore(card, turn.body);
  turn.gate = card;

  const branches = card.querySelector(".gc-branches");
  card.querySelector("[data-approve]").addEventListener("click", () => resume(turn, proposed));
  card.querySelector("[data-change]").addEventListener("click", () => {
    branches.hidden = !branches.hidden;
    scrollToBottom(true);
  });
  card.querySelector("[data-cancel]").addEventListener("click", () => {
    card.className = "gatecard resolved dropped";
    card.innerHTML = '<p class="gc-resolved">run abandoned at the gate</p>';
    setStep(turn, "approve", "cancelled");
    setNode("approval", "idle");
    setGraphState("idle");
    turn.root.classList.remove("is-live");
    clearWork(turn);
    finishTurn();
  });
  card.querySelectorAll(".gc-branch").forEach((btn) => {
    btn.addEventListener("click", () => resume(turn, btn.dataset.category));
  });
}

function resume(turn, category) {
  const meta = CATEGORY_BY_ID[category];
  if (turn.gate) {
    turn.gate.className = "gatecard resolved";
    turn.gate.innerHTML = `<p class="gc-resolved">released at the gate → <strong>${escapeHtml(
      meta ? meta.label : category
    )}</strong></p>`;
  }
  setStep(turn, "approve", "done");
  setStep(turn, "analyze", "active");
  setWork(turn, "resuming from the gate");
  socket.emit("resume", { run_id: turn.runId, category });
}

// ── Graph map (inspector) ────────────────────────────────────────────────
function resetGraph(category) {
  graphMap.querySelectorAll("[data-node]").forEach((el) => (el.dataset.state = "idle"));
  if (category !== "auto") {
    setNode("router", "skipped");
    setNode("approval", "skipped");
  } else if (!state.requireApproval) {
    setNode("approval", "skipped");
  }
}

function setNode(node, status) {
  const el = graphMap.querySelector(`[data-node="${node}"]`);
  if (!el) return;
  if (el.dataset.state === "done" && status === "active") return;
  el.dataset.state = status;
}

function highlightLeaf(category) {
  graphMap.querySelectorAll(".map-leaf").forEach((el) => {
    if (el.dataset.node !== category && el.dataset.state !== "idle") el.dataset.state = "idle";
  });
  setNode(category, "active");
}

function setGraphState(text) {
  graphState.textContent = text;
  graphState.dataset.live = text !== "idle" ? "1" : "0";
}

// ── Route picker ─────────────────────────────────────────────────────────
function buildRouteMenu() {
  const row = (id, label, desc, tone) => `
    <button class="pick-row${state.category === id ? " on" : ""}" data-category="${id}" data-tone="${tone}" role="option">
      <span class="pr-dot"></span>
      <span class="pr-text"><strong>${label}</strong><em>${desc}</em></span>
    </button>`;

  routeMenu.innerHTML = `
    <div class="pick-head">router decides</div>
    ${row("auto", "auto", "LLM classifies the question", "amber")}
    <div class="pick-head">skip the router — enter at</div>
    ${CATEGORIES.map((c) => row(c.id, escapeHtml(c.label.toLowerCase()), escapeHtml(c.desc), c.tone)).join("")}
  `;

  routeMenu.querySelectorAll(".pick-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      setCategory(btn.dataset.category);
      closeRouteMenu();
    });
  });
}

function setCategory(id) {
  state.category = id;
  const meta = CATEGORY_BY_ID[id];
  routePickerLabel.textContent = meta ? meta.label.toLowerCase() : "auto";
  routePickerBtn.dataset.tone = meta ? meta.tone : "amber";
  routePickerBtn.classList.toggle("is-manual", id !== "auto");

  const manual = id !== "auto";
  approvalSwitch.classList.toggle("off", manual);
  approvalToggle.disabled = manual;
  sessionSub.textContent = manual
    ? `direct → ${meta.label.toLowerCase()} · router skipped`
    : `auto-route · approval ${state.requireApproval ? "on" : "off"}`;

  document.querySelectorAll(".node-row").forEach((el) => {
    el.classList.toggle("on", el.dataset.category === id);
  });
  buildRouteMenu();
}

function openRouteMenu() {
  routeMenu.hidden = false;
  routePickerBtn.setAttribute("aria-expanded", "true");
}

function closeRouteMenu() {
  routeMenu.hidden = true;
  routePickerBtn.setAttribute("aria-expanded", "false");
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

routePickerBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  routeMenu.hidden ? openRouteMenu() : closeRouteMenu();
});

document.addEventListener("click", (e) => {
  if (!routeMenu.hidden && !routeMenu.contains(e.target)) closeRouteMenu();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeRouteMenu();
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    routeMenu.hidden ? openRouteMenu() : closeRouteMenu();
  }
});

approvalToggle.addEventListener("change", () => {
  state.requireApproval = approvalToggle.checked;
  setCategory(state.category);
});

document.querySelectorAll(".node-row").forEach((btn) => {
  btn.addEventListener("click", () => {
    setCategory(btn.dataset.category);
    submitQuery(btn.dataset.query || undefined, btn.dataset.category);
    closeDockOnMobile();
  });
});

document.querySelectorAll(".op-card").forEach((btn) => {
  btn.addEventListener("click", () => {
    setCategory(btn.dataset.category);
    submitQuery(btn.dataset.query, btn.dataset.category);
  });
});

clearChatBtn.addEventListener("click", () => {
  messagesList.innerHTML = "";
  if (welcomeHero) welcomeHero.hidden = false;
  showToast("Ledger cleared", "info");
});

newChatBtn.addEventListener("click", () => {
  state.sessionId = newId();
  state.indexed = [];
  state.pending = [];
  state.runNo = 0;
  messagesList.innerHTML = "";
  if (welcomeHero) welcomeHero.hidden = false;
  statRuns.textContent = "0";
  sourceCount.textContent = "00";
  inspSources.innerHTML = '<p class="insp-empty">Passages pulled for the active run land here.</p>';
  resetGraph("auto");
  setGraphState("idle");
  renderTray();
  renderFilings();
  stampSession();
  closeDockOnMobile();
  showToast("New dossier opened", "success");
});

attachBtn.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});

fileInput.addEventListener("change", (e) => {
  if (e.target.files?.length) stageFiles(e.target.files);
  fileInput.value = "";
});

let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
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
inspectorToggle.addEventListener("click", () => {
  inspector.classList.toggle("collapsed");
  inspectorToggle.classList.toggle("off", inspector.classList.contains("collapsed"));
});

function closeDockOnMobile() {
  dock.classList.remove("open");
}

// ── Utilities ────────────────────────────────────────────────────────────
function scrollToBottom(force = false) {
  const nearBottom =
    chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight < 240;
  if (force || nearBottom) chatContainer.scrollTop = chatContainer.scrollHeight;
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
  sessionIdEl.textContent = `DOSSIER ${state.sessionId.slice(0, 6).toUpperCase()}`;
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

async function checkBackendHealth() {
  try {
    const res = await fetch(`${BACKEND_URL}/api/health`);
    if (!res.ok) return;
    const data = await res.json();
    serverStatusText.textContent = "linked";
    modelTag.textContent = data.model.replace(":latest", "");
  } catch {
    /* surfaced by the socket handlers */
  }
}

// ── Init ─────────────────────────────────────────────────────────────────
setCategory("auto");
renderFilings();
renderTray();
stampSession();
setGraphState("idle");
checkBackendHealth();
autoGrow();
