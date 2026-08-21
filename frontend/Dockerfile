# syntax=docker/dockerfile:1

# Corporate Filing Analyzer — workbench image.
#
# The client is plain HTML/CSS/JS with its two dependencies vendored in
# (socket.io.min.js, marked.min.js), so there is nothing to bundle and no build
# step: this is nginx serving a directory, plus a reverse proxy that puts the
# API on the same origin as the page.

# The unprivileged variant listens on 8080 and runs as uid 101 out of the box,
# rather than starting as root to bind port 80 and dropping afterwards.
FROM nginxinc/nginx-unprivileged:1.29-alpine

COPY --chown=nginx:nginx nginx.conf /etc/nginx/conf.d/default.conf

COPY --chown=nginx:nginx index.html style.css app.js auth.js \
     socket.io.min.js marked.min.js /usr/share/nginx/html/

# Takes the place of the checked-in development config: in here the API is on
# this page's own origin, because of the proxy above. Copied rather than written
# with a RUN — this image runs as an unprivileged user that cannot write into
# the root-owned web root.
COPY --chown=nginx:nginx config.docker.js /usr/share/nginx/html/config.js

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/ || exit 1
