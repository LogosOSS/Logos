# ──────────────────────────────────────────────────────────
# Stage 1: Builder – compile wheels and install dependencies
# ──────────────────────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

WORKDIR /build/logos/logos-orchestrator

# apt installs exact versions from a fixed Debian archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together.
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i -E \
      -e "s#https?://deb\.debian\.org/debian-security/?#https://snapshot.debian.org/archive/debian-security/${APT_SNAPSHOT}/#g" \
      -e "s#https?://deb\.debian\.org/debian/?#https://snapshot.debian.org/archive/debian/${APT_SNAPSHOT}/#g" \
      /etc/apt/sources.list.d/debian.sources \
  && ! grep -q deb.debian.org /etc/apt/sources.list.d/debian.sources

# Install system packages needed for compiling C-extension wheels
RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3 update \
  && apt-get install -y --no-install-recommends \
       gcc=4:14.2.0-1 build-essential=12.12 \
       rustc=1.85.1+dfsg1-1+deb13u1 cargo=1.85.1+dfsg1-1+deb13u1 \
  && rm -rf /var/lib/apt/lists/*

# Install uv – a drop-in pip replacement written in Rust (10-100× faster)
# Pinned to a specific version to mitigate supply-chain risk (update explicitly)
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

# Create an isolated virtual-env that we will copy into the runtime image
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# `uv sync` manages a project environment; point it at the venv above rather
# than letting it create .venv next to the project.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# ── Dependency layer (cached unless pyproject.toml or uv.lock changes) ──
COPY logos-orchestrator/pyproject.toml logos-orchestrator/uv.lock ./

# Create a minimal package stub so the poetry-core build backend can
# resolve metadata without needing the real source tree.
RUN mkdir -p src/logos && echo '__version__ = "0.1.0"' > src/logos/__init__.py \
 && touch README.md

# Install all dependencies with uv (replaces poetry install ~436 s → ~30-60 s)
#
# `uv sync --locked` installs exactly what uv.lock pins and fails if the lock
# has drifted from pyproject.toml, so the image can no longer quietly resolve
# a different dependency set than the one that was reviewed and tested.
# --no-install-project: only the dependencies belong in this cached layer; the
# service's own source is copied into the runtime stage and imported via
# PYTHONPATH, so there is nothing to install for it here.
# sentence-transformers pulls in torch as a transitive dep; this container has
# no GPU (see docker-compose.yaml — no device reservation for this service),
# so pull the CPU-only build instead of PyPI's default CUDA wheels (which drag
# in ~15 nvidia-cu12 packages and blow the install up from ~1 min to ~8 min).
# torch is pinned to PyTorch's CPU-only wheel index via tool.uv.sources + an
# explicit index in pyproject.toml, so no extra install-time flags are needed
# here to steer it away from PyPI's default CUDA build.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --compile-bytecode

# ──────────────────────────────────────────────────────────
# Stage 2: Runtime – slim image without compilers or build tools
# ──────────────────────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# apt installs exact versions from a fixed Debian archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together.
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i -E \
      -e "s#https?://deb\.debian\.org/debian-security/?#https://snapshot.debian.org/archive/debian-security/${APT_SNAPSHOT}/#g" \
      -e "s#https?://deb\.debian\.org/debian/?#https://snapshot.debian.org/archive/debian/${APT_SNAPSHOT}/#g" \
      /etc/apt/sources.list.d/debian.sources \
  && ! grep -q deb.debian.org /etc/apt/sources.list.d/debian.sources

# Only install runtime system dependencies (curl for health-checks)
RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3 update \
  && apt-get install -y --no-install-recommends curl=8.14.1-2+deb13u7 \
  && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtual-env from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy source code
COPY logos-orchestrator/src/ ./src/

# Generate gRPC python code from proto(s)
RUN if [ -f src/grpclocal/model.proto ]; then \
      python -m grpc_tools.protoc -I src --python_out=src --grpc_python_out=src src/grpclocal/model.proto; \
    fi

# Copy other project files
COPY db/ ./logos/db/

# Compile application bytecode before switching to the runtime user.
RUN python -m compileall -q /app/src

# The classifier cache is relative to /app; model downloads use the user's home.
RUN groupadd --system --gid 10001 logos \
  && useradd --uid 10001 --gid logos --create-home --no-log-init logos \
  && chown logos:logos /app
ENV HOME=/home/logos
USER 10001:10001

EXPOSE 8080 50051

CMD ["uvicorn", "logos.main:app", "--host", "0.0.0.0", "--port", "8080", "--ws-ping-interval", "60", "--ws-ping-timeout", "60", "--ws-max-size", "268435456"]
