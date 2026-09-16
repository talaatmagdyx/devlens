# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# The UI is compiled first and copied *into the package*, so an installed wheel
# and this image both carry it. The previous image built the UI and then put it
# where the application does not look, and served nothing.
# ---------------------------------------------------------------------------
# The base images are arguments so the same Dockerfile can be built where a
# registry is unreachable — against a locally constructed base — and in CI
# against the published ones. The defaults are what ships.
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM ${PYTHON_IMAGE} AS runtime

# git and ripgrep are not optional extras: the analyze, review and implement
# workflows shell out to them, and DevLens reports a missing binary as a
# provider error rather than pretending the search found nothing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=web /web/dist ./src/devlens/web/dist

RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home devlens \
    && mkdir -p /data /workspace \
    && chown -R devlens:devlens /data /workspace

USER 10001:10001

# The UI is found inside the installed package, so no path needs to be
# configured here. DEVLENS_UI_DIR remains available to point at a different
# build.
ENV DEVLENS_JOBS_DB=/data/jobs.sqlite \
    DEVLENS_ANALYZE_ROOT=/workspace \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Declared so a named volume inherits the ownership prepared above. A tmpfs or
# a bind mount does not: mount those writable by uid 10001, or point
# DEVLENS_JOBS_DB elsewhere. DevLens says so by name if you get it wrong.
VOLUME ["/data"]

EXPOSE 8000

# Every optional capability stays off unless the operator sets its variable.
# Nothing here enables writes, repository code execution or a language model.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if b'ok' in urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read() else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "devlens.app.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
