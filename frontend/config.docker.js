/**
 * The in-container counterpart to config.js.
 *
 * Empty string means "the API is on this page's own origin", which holds
 * because nginx proxies /api and /socket.io through to the backend. Copied over
 * config.js when the image is built; the repo copy stays unset so a plain dev
 * server still reaches http://localhost:8000.
 */
window.__BACKEND_URL__ = "";
