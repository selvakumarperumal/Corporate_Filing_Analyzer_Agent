/**
 * Where the client looks for the API.
 *
 * Left unset on purpose. Served from a plain dev server, the app falls back to
 * http://localhost:8000 (see BACKEND_URL in app.js), which is where the backend
 * runs when it is started by hand.
 *
 * The Docker image replaces this file with one that sets an empty string,
 * meaning "the same origin as this page" — nginx there proxies /api and
 * /socket.io through to the backend, so the browser only ever sees one origin.
 * Set it to an absolute URL to point a local page at a remote backend:
 *
 *     window.__BACKEND_URL__ = "https://analyzer.example.com";
 */
