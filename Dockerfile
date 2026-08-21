# Two stages: build wheels once, then copy only what runs.
# The result carries no compiler and no build cache.
FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip build \
 && pip wheel --no-cache-dir --wheel-dir /wheels ".[cli,api,yaml,http,postgres]"

FROM python:3.12-slim

# Patch the base layer before anything else. The python:slim images lag
# Debian security updates by days to weeks, and a scan gate that fails on
# util-linux CVEs the base has not picked up yet is a gate people learn to
# override. Applying available security updates at build time is cheap and
# keeps the gate meaningful.
RUN apt-get update  && apt-get upgrade -y --no-install-recommends  && rm -rf /var/lib/apt/lists/*

# Runs as a non-root user. The orchestrator executes tools; if one of them is
# ever made to do something unintended, it should be unintended as nobody in
# particular rather than as root.
RUN useradd --create-home --uid 10001 orchestrator

WORKDIR /app
COPY --from=build /wheels /wheels
# cli is included because the image's own CMD is `orchestrator serve`, and
# the migration service runs `orchestrator migrate` — both are the typer
# CLI. postgres is included because the production Compose stack sets
# storage.backend: postgres; without asyncpg the container starts and then
# fails at first connection.
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
      universal-orchestrator[cli,api,yaml,http,postgres] \
 && rm -rf /wheels

COPY ui/ ./ui/
COPY workflows/ ./workflows/

# State lives on a volume. Without one, every restart loses the audit trail —
# which is the one thing in this system that is supposed to be durable.
RUN mkdir -p /data/.orchestrator && chown -R orchestrator:orchestrator /data /app
VOLUME ["/data"]
WORKDIR /data
USER orchestrator

EXPOSE 8080

# Liveness only. Readiness depends on models being reachable, and a container
# runtime that restarts on readiness turns a brief provider outage into a
# crash loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/live',timeout=4).status==200 else 1)"

# 0.0.0.0 is required inside a container, so ORCHESTRATOR_API_TOKEN must be
# set or the process refuses to start. That refusal is deliberate: see
# src/orchestrator/api/security.py.
ENV ORCHESTRATOR_STATE_DIR=/data/.orchestrator
CMD ["orchestrator", "serve", "--host", "0.0.0.0", "--port", "8080"]
