# ──────────────────────────────────────────────────────────
# Stage 1: Builder – resolve dependencies into a virtual env
# ──────────────────────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

WORKDIR /build

# apt installs exact versions from a fixed Debian archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together.
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i \
      -e "s#http://deb.debian.org/debian-security#http://snapshot.debian.org/archive/debian-security/${APT_SNAPSHOT}#" \
      -e "s#http://deb.debian.org/debian#http://snapshot.debian.org/archive/debian/${APT_SNAPSHOT}#" \
      /etc/apt/sources.list.d/debian.sources \
  && ! grep -q deb.debian.org /etc/apt/sources.list.d/debian.sources

RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3 update \
  && apt-get install -y --no-install-recommends gcc=4:14.2.0-1 build-essential=12.12 \
  && rm -rf /var/lib/apt/lists/*

# Pinned like the orchestrator's build, so a uv release cannot change what
# lands in the image without an explicit bump here.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# `uv sync` manages a project environment; point it at the venv above.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Exactly the versions in uv.lock (--locked fails if it drifted from
# pyproject.toml). The service runs from the source tree copied into the
# runtime stage (`uvicorn app.main:app` from /srv), so only the dependencies
# are installed, never the project itself.
COPY logos-agent/pyproject.toml logos-agent/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# ──────────────────────────────────────────────────────────
# Stage 2: Runtime
# ──────────────────────────────────────────────────────────
FROM python:3.13-slim

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY logos-agent/app ./app

# The service needs the Docker socket to create session containers, which is a
# privileged capability: it runs as root inside its own container for that
# reason, and holds no other elevated access. The session containers it
# creates get neither the socket nor root — see app/docker_engine.py.
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]
