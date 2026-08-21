/**
 * Corporate Filing Analyzer — sign-in gate and token lifecycle
 *
 * Holds an access/refresh pair for the workbench. The access token is short
 * lived and goes out on every request; the refresh token is spent to mint a
 * new pair and is rotated each time, so the one in storage is only ever the
 * newest.
 *
 * Two things keep a session alive without the analyst noticing:
 *
 *   - a timer that refreshes shortly before the access token expires, so a
 *     request rarely meets a 401 in the first place;
 *   - `authFetch`, which on a 401 refreshes once and retries, so the run that
 *     happened to straddle an expiry still lands.
 *
 * Everything the rest of the app needs is on `window.Auth`; sign-in and
 * sign-out are announced as `auth:signedin` / `auth:signedout` events rather
 * than by calling into app.js, so neither file has to know the other's shape.
 */

const Auth = (() => {
  const STORE_KEY = "cfa.session";

  // Refresh this far ahead of expiry: long enough to cover a slow round trip,
  // short enough that a token is not replaced while it still has real life.
  const REFRESH_LEAD_MS = 60_000;
  const MIN_REFRESH_DELAY_MS = 5_000;

  // Same rule as app.js: config.js may name the backend, and the Docker image
  // sets it to "" for this page's own origin. Resolved independently because
  // auth.js loads first and must not depend on app.js having run.
  const BACKEND_URL = (() => {
    if (typeof window.__BACKEND_URL__ === "string") {
      return window.__BACKEND_URL__ || window.location.origin;
    }
    return window.location.port === "8000"
      ? window.location.origin
      : "http://localhost:8000";
  })();

  let session = null;      // { access_token, refresh_token, user }
  let refreshTimer = null;
  // One in-flight refresh at a time: a page that fires three requests into an
  // expired token must not spend the refresh token three times — the first two
  // rotations would invalidate the third and log the analyst out.
  let refreshing = null;

  // ── Storage ────────────────────────────────────────────────────────────
  function load() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    } catch {
      return null;
    }
  }

  function save(pair) {
    session = {
      access_token: pair.access_token,
      refresh_token: pair.refresh_token,
      user: pair.user,
    };
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(session));
    } catch {
      // Private-browsing modes refuse to write. The session still works for
      // this tab; it just will not survive a reload.
    }
    scheduleRefresh(pair.expires_in);
  }

  function clear() {
    session = null;
    clearTimeout(refreshTimer);
    refreshTimer = null;
    try {
      localStorage.removeItem(STORE_KEY);
    } catch {}
  }

  // ── Token lifecycle ────────────────────────────────────────────────────
  function scheduleRefresh(expiresIn) {
    clearTimeout(refreshTimer);
    if (!expiresIn) return;
    const delay = Math.max(expiresIn * 1000 - REFRESH_LEAD_MS, MIN_REFRESH_DELAY_MS);
    refreshTimer = setTimeout(() => {
      refresh().catch(() => {
        // The refresh token is dead — nothing left to do but sign in again.
        signOutLocally("Your session expired — please sign in again.");
      });
    }, delay);
  }

  /**
   * Spend the refresh token for a new pair.
   *
   * Concurrent callers share the one request: `refreshing` is the promise of
   * the rotation already under way, not a second one.
   */
  function refresh() {
    if (refreshing) return refreshing;
    if (!session?.refresh_token) return Promise.reject(new Error("Not signed in"));

    refreshing = post("/api/auth/refresh", { refresh_token: session.refresh_token })
      .then((pair) => {
        save(pair);
        return pair;
      })
      .finally(() => {
        refreshing = null;
      });

    return refreshing;
  }

  /**
   * fetch() with the access token attached, refreshing once on a 401.
   *
   * The retry is deliberately capped at one: if a freshly minted access token
   * is still refused, the problem is not staleness and retrying would only
   * spin.
   */
  async function authFetch(path, options = {}, retry = true) {
    const headers = new Headers(options.headers || {});
    if (session?.access_token) {
      headers.set("Authorization", `Bearer ${session.access_token}`);
    }

    const response = await fetch(`${BACKEND_URL}${path}`, { ...options, headers });

    if (response.status === 401 && retry && session?.refresh_token) {
      try {
        await refresh();
      } catch {
        signOutLocally("Your session expired — please sign in again.");
        return response;
      }
      return authFetch(path, options, false);
    }

    return response;
  }

  /** POST JSON to the API without a token — the auth endpoints themselves. */
  async function post(path, body) {
    const response = await fetch(`${BACKEND_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    // A failure can arrive as something other than JSON (a proxy error page),
    // and parsing that would replace the real reason with a parser error.
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(readError(data, response));
    return data;
  }

  /**
   * The reason a request failed, in one sentence.
   *
   * FastAPI reports a rejected body as a list of per-field problems; the first
   * one is what the analyst needs to fix, and the rest follow from it.
   */
  function readError(data, response) {
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0];
      const field = first?.loc?.[first.loc.length - 1];
      const message = first?.msg?.replace(/^Value error, /, "") || "is not valid";
      return field ? `${String(field)}: ${message}` : message;
    }
    return `Request failed (${response.status})`;
  }

  // ── Entry points ───────────────────────────────────────────────────────
  async function signup({ email, name, password }) {
    save(await post("/api/auth/signup", { email, name, password }));
    announceIn();
  }

  async function login({ email, password }) {
    save(await post("/api/auth/login", { email, password }));
    announceIn();
  }

  /**
   * End the session on the server as well as here.
   *
   * The local half happens whatever the request does: a logout that leaves the
   * analyst signed in because the network was down is worse than one whose
   * refresh token outlives its own expiry unrevoked.
   */
  async function logout() {
    const token = session?.refresh_token;

    // Announced before the token is thrown away: the workbench has requests of
    // its own to get out (discarding this session's filings) and they need a
    // credential that is still good. Listeners are called synchronously, so
    // those requests are already on the wire by the time `clear()` runs.
    window.dispatchEvent(new CustomEvent("auth:signingout"));

    clear();
    announceOut();
    if (token) {
      try {
        await fetch(`${BACKEND_URL}/api/auth/logout`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: token }),
          keepalive: true,
        });
      } catch {
        // The token expires on its own soon enough.
      }
    }
  }

  /** Drop the session here because the server will no longer honour it. */
  function signOutLocally(message) {
    clear();
    announceOut(message);
  }

  /**
   * Restore a stored session, or show the gate.
   *
   * The stored access token is not trusted on its own — it may well have
   * expired while the tab was closed — so the refresh token is spent for a
   * fresh pair, which doubles as proof the session is still good.
   */
  async function boot() {
    session = load();
    if (!session?.refresh_token) {
      announceOut();
      return;
    }

    try {
      await refresh();
      announceIn();
    } catch {
      signOutLocally();
    }
  }

  function announceIn() {
    window.dispatchEvent(
      new CustomEvent("auth:signedin", { detail: { user: session.user } })
    );
  }

  function announceOut(message) {
    window.dispatchEvent(new CustomEvent("auth:signedout", { detail: { message } }));
  }

  return {
    boot,
    login,
    signup,
    logout,
    refresh,
    authFetch,
    backendUrl: BACKEND_URL,
    get user() {
      return session?.user || null;
    },
    get accessToken() {
      return session?.access_token || null;
    },
    get isSignedIn() {
      return Boolean(session?.access_token);
    },
  };
})();

// ══ Gate UI ════════════════════════════════════════════════════════════════
(() => {
  const $ = (id) => document.getElementById(id);

  const gate = $("authGate");
  const form = $("authForm");
  const nameField = $("authNameField");
  const nameInput = $("authName");
  const emailInput = $("authEmail");
  const passwordInput = $("authPassword");
  const submitBtn = $("authSubmit");
  const submitLabel = $("authSubmitLabel");
  const errorBox = $("authError");
  const switchBtn = $("authSwitch");
  const switchNote = $("authSwitchNote");
  const headline = $("authHeadline");
  const stepLabel = $("authStep");
  const hint = $("authHint");

  let mode = "login";  // or "signup"
  let busy = false;

  function setMode(next) {
    mode = next;
    const signup = mode === "signup";

    nameField.hidden = !signup;
    nameInput.required = signup;
    headline.innerHTML = signup
      ? 'Open an <em>account.</em>'
      : 'Sign in to the <em>workbench.</em>';
    stepLabel.textContent = signup ? "NEW ANALYST" : "IDENTIFY";
    submitLabel.textContent = signup ? "CREATE ACCOUNT" : "SIGN IN";
    switchNote.textContent = signup ? "Already registered?" : "No account yet?";
    switchBtn.textContent = signup ? "Sign in" : "Open one";
    passwordInput.autocomplete = signup ? "new-password" : "current-password";
    hint.textContent = signup
      ? "at least 8 characters"
      : "dossiers and filings are private to your account";

    showError("");
    (signup ? nameInput : emailInput).focus();
  }

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  function setBusy(next) {
    busy = next;
    submitBtn.disabled = next;
    gate.classList.toggle("is-busy", next);
    submitLabel.textContent = next
      ? mode === "signup"
        ? "CREATING…"
        : "SIGNING IN…"
      : mode === "signup"
      ? "CREATE ACCOUNT"
      : "SIGN IN";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;

    const email = emailInput.value.trim();
    const password = passwordInput.value;
    const name = nameInput.value.trim();

    if (mode === "signup" && password.length < 8) {
      showError("Password must be at least 8 characters.");
      passwordInput.focus();
      return;
    }

    showError("");
    setBusy(true);
    try {
      if (mode === "signup") await Auth.signup({ email, name, password });
      else await Auth.login({ email, password });
      form.reset();
    } catch (error) {
      // fetch rejects with a TypeError when the request never reached the
      // backend — worth saying, since it is not the analyst's credentials.
      showError(
        error instanceof TypeError
          ? "Can't reach the analyzer — check the backend is running."
          : error.message
      );
      passwordInput.select();
    } finally {
      setBusy(false);
    }
  });

  switchBtn.addEventListener("click", () => setMode(mode === "login" ? "signup" : "login"));

  window.addEventListener("auth:signedin", () => {
    gate.hidden = true;
    document.body.classList.remove("is-locked");
  });

  window.addEventListener("auth:signedout", (event) => {
    gate.hidden = false;
    document.body.classList.add("is-locked");
    setMode("login");
    if (event.detail?.message) showError(event.detail.message);
  });

  setMode("login");
  Auth.boot();
})();
