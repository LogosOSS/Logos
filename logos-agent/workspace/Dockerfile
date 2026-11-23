# The image an agent session runs in.
#
# It holds a coding agent, the tools it needs to do development work, and a
# browser for screenshots. It deliberately holds no credentials and no Docker
# client: everything privileged is done by the runner service outside this
# container (see app/docker_engine.py for the sandbox this image runs under).
# Resolve the same Maven toolchain and project dependencies as the application
# image while build-time network access is available. Running test-compile
# warms compiler/plugin dependencies that dependency:go-offline can miss.
FROM maven:3.10-eclipse-temurin-25@sha256:37ecd7dbbe926e9b69586747a25164c0f3b764455072322d25cc7724ee9e1b64 AS java-verification
COPY logos-webservice/install-temurin.sh /tmp/install-temurin.sh
RUN sh /tmp/install-temurin.sh jdk && rm /tmp/install-temurin.sh
ENV JAVA_VERSION=jdk-25.0.5.1+2
WORKDIR /build/logos-webservice
COPY logos-webservice/pom.xml ./pom.xml
COPY logos-webservice/src ./src
RUN mvn --batch-mode --no-transfer-progress \
      -Dmaven.repo.local=/opt/logos-verification/maven-repository test-compile
RUN --network=none rm -rf target \
  && mvn --offline --batch-mode --no-transfer-progress \
       -Dmaven.repo.local=/opt/logos-verification/maven-repository test-compile

# Keep the content-addressed npm cache so npm ci can validate each session's
# actual lockfile offline; changed dependency versions fail instead of using
# node_modules from a different revision.
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS ui-verification
WORKDIR /build/logos-ui
COPY logos-ui/package.json logos-ui/package-lock.json ./
RUN npm ci --cache /opt/logos-verification/npm-cache --no-audit --no-fund
COPY logos-ui/src ./src
COPY logos-ui/public ./public
COPY logos-ui/angular.json logos-ui/tsconfig.json logos-ui/tsconfig.app.json logos-ui/tsconfig.spec.json ./
COPY logos-ui/postcss.config.mjs logos-ui/vitest-base.config.mts ./
ENV CI=true NG_CLI_ANALYTICS=false LANG=C.UTF-8
RUN --network=none npm ci --offline --cache /opt/logos-verification/npm-cache --no-audit --no-fund \
  && node node_modules/@angular/cli/bin/ng.js build --configuration production \
  && node node_modules/@angular/cli/bin/ng.js test --watch=false

FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Playwright's browsers go somewhere the unprivileged user owns and the
    # read-only root filesystem still permits at build time.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# apt installs exact versions from a fixed Debian archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together. Snapshot lists are older than their Valid-Until date by
# design, hence the apt setting. The snapshot is fetched over HTTPS, and the
# slim Node.js base has no CA bundle yet (the ca-certificates package below
# installs the real one), so borrow the bundle of a digest-pinned image; the
# packages themselves are still verified against Debian's signed InRelease.
COPY --from=python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 \
     /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i -E \
      -e "s#https?://deb\.debian\.org/debian-security/?#https://snapshot.debian.org/archive/debian-security/${APT_SNAPSHOT}/#g" \
      -e "s#https?://deb\.debian\.org/debian/?#https://snapshot.debian.org/archive/debian/${APT_SNAPSHOT}/#g" \
      /etc/apt/sources.list.d/debian.sources \
  && ! grep -q deb.debian.org /etc/apt/sources.list.d/debian.sources \
  && printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99snapshot

RUN apt-get update && apt-get install -y --no-install-recommends \
      git=1:2.39.5-0+deb12u3 \
      openssh-client=1:9.2p1-2+deb12u10 \
      ca-certificates=20250419~deb12u2 \
      curl=7.88.1-10+deb12u15 \
      gnupg=2.2.40-1.1+deb12u2 \
      jq=1.6-2.1+deb12u3 \
      ripgrep=13.0.0-4+b2 \
      python3=3.11.2-1+b1 \
      python3-venv=3.11.2-1+b1 \
      python3-pip=23.0.1+dfsg-1 \
      # Chromium's runtime dependencies. Installed explicitly rather than via
      # `playwright install-deps` so the layer is reproducible.
      libnss3=2:3.87.1-1+deb12u4 libnspr4=2:4.35-1 libatk1.0-0=2.46.0-5 \
      libatk-bridge2.0-0=2.46.0-5 libcups2=2.4.2-3+deb12u9 libdrm2=2.4.114-1+b1 \
      libxkbcommon0=1.5.0-1 libxcomposite1=1:0.4.5-1 libxdamage1=1:1.1.6-1 \
      libxfixes3=1:6.0.0-2 libxrandr2=2:1.5.2-2+b1 libgbm1=22.3.6-1+deb12u2 \
      libpango-1.0-0=1.50.12+ds-1 libcairo2=1.16.0-7 libasound2=1.2.8-1+b1 \
  && rm -rf /var/lib/apt/lists/*

# A JDK, so a session can build and test logos-webservice. It targets Java 25
# (see logos-webservice/pom.xml), which Debian bookworm does not package at all,
# so it comes from Adoptium rather than from the distribution. The service's
# Maven distribution and offline repository come from the build stage below.
ARG JDK_VERSION=25
# The exact package version from Adoptium's repository (it keeps old releases).
ARG JDK_PACKAGE_VERSION=25.0.5.1.0+2-0
RUN curl --proto =https -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
      | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \
  && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
      > /etc/apt/sources.list.d/adoptium.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends "temurin-${JDK_VERSION}-jdk=${JDK_PACKAGE_VERSION}" \
  && java -version 2>&1 | grep -F 'build 25.0.5.1+2' \
  && rm -rf /var/lib/apt/lists/*

# GitHub CLI, the release package of an exact version verified against its
# SHA-256. The official apt repository only serves the newest release, so a
# version pinned there stops resolving with the next gh release. Its only
# dependency, git, is installed above. Bump GH_VERSION and both hashes together
# (from the release's gh_<version>_checksums.txt).
ARG GH_VERSION=2.104.0
ARG GH_SHA256_AMD64=f90d578cbc4c3b067dfc4a1a540cef41faef64eebfb280b548ab32455977a21f
ARG GH_SHA256_ARM64=95b0632a7e1b12761248b7266825b0eab0853cefb84a1e272d905acb6e436f6c
RUN arch="$(dpkg --print-architecture)" \
  && case "$arch" in \
       amd64) sha256="$GH_SHA256_AMD64" ;; \
       arm64) sha256="$GH_SHA256_ARM64" ;; \
       *) echo "no pinned gh package for $arch" >&2; exit 1 ;; \
     esac \
  && curl --proto =https -fsSL -o /tmp/gh.deb \
       "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.deb" \
  && echo "${sha256}  /tmp/gh.deb" | sha256sum -c - \
  && dpkg -i /tmp/gh.deb \
  && rm /tmp/gh.deb \
  && gh --version | grep -q "gh version ${GH_VERSION} "

# uv, matching the pin used by the other Logos images.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

# Logos targets Python 3.13; Debian bookworm ships 3.11. Install the interpreter
# at build time into a shared location, so a session runs `uv venv --python
# 3.13` instantly instead of downloading ~30 MB on every run — and so it works
# at all, since the session's root filesystem is read-only.
#
# 3.12 as well, and not as a spare: Athena's nbstripout hook pins
# `language_version: python3.12`, and without that interpreter its hook
# environment cannot be built at all.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.13.18 3.12.16 && chmod -R a+rX /opt/uv-python

# The coding agent itself — this image exists to run Claude Code — and
# Playwright for screenshots. Both pinned so a session's behaviour does not
# change under us between builds; bump them deliberately.
ARG CLAUDE_CODE_VERSION=2.1.245
ARG PLAYWRIGHT_VERSION=1.62.1
RUN npm install -g \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "playwright@${PLAYWRIGHT_VERSION}" \
  && npm cache clean --force

RUN npx --yes playwright@${PLAYWRIGHT_VERSION} install chromium \
  && chmod -R a+rX /opt/playwright

# The repository's own linters, installed at build time because a session has
# no network to install them with. Without this an agent cannot run what the
# house rules ask it to run: `pre-commit` fetches every hook from GitHub the
# first time, `pip install black` reaches PyPI, and the sandbox reaches
# neither. Sessions were pushing unformatted code and finding out from a red
# check nobody told them about.
#
# Logos' hooks are `language: system` and run the tools locked in
# tools/pre-commit/requirements.lock, all installed into this venv, so they
# need no hook environments; install-hooks still validates the copied config
# and prepares the store at PRE_COMMIT_STORE.
# A venv of its own at the shared location the other Logos images use, not
# a tool install under /root: the session runs as `agent`, and everything it
# needs has to be readable without opening up root's home. Deliberately no
# VIRTUAL_ENV — this venv holds the linters, and a session building the
# project makes its own; pointing uv at this one would only confuse it.
ENV PATH=/opt/venv/bin:$PATH \
    PRE_COMMIT_STORE=/opt/pre-commit
COPY .pre-commit-config.yaml /tmp/hooks/.pre-commit-config.yaml
COPY tools/pre-commit/requirements.lock /tmp/hooks/pre-commit.lock
RUN uv venv --python 3.13.18 /opt/venv \
  && uv pip install --python /opt/venv --no-deps --require-hashes -r /tmp/hooks/pre-commit.lock \
  && uv pip check --python /opt/venv \
  && cd /tmp/hooks \
  && git init -q . \
  && git config user.email agent@logos.invalid \
  && git config user.name agent \
  && PRE_COMMIT_HOME=/opt/pre-commit pre-commit install-hooks --config .pre-commit-config.yaml \
  && chmod -R a+rX /opt/pre-commit /opt/venv \
  && rm -rf /tmp/hooks

COPY --from=java-verification /usr/share/maven /usr/share/maven
COPY --from=java-verification /opt/logos-verification/maven-repository /opt/logos-verification/maven-repository
COPY --from=ui-verification /opt/logos-verification/npm-cache /opt/logos-verification/npm-cache
ENV PATH=/usr/share/maven/bin:$PATH
RUN chmod -R a+rX /usr/share/maven /opt/logos-verification

# Unprivileged user. The runner starts the container as `agent`; nothing in a
# session ever runs as root.
# /cache is the mount point of a linked repository's workspace cache. Docker
# fills an empty named volume from the image directory it is mounted over,
# ownership included, so a fresh cache volume belongs to the session user.
RUN useradd --create-home --shell /bin/bash --uid 10001 agent \
  && mkdir -p /workspace /artifacts /cache \
  && chown -R agent:agent /workspace /artifacts /cache /home/agent

COPY --chown=agent:agent logos-agent/workspace/run_session.py /usr/local/lib/run_session.py
COPY --chown=agent:agent logos-agent/workspace/verify_checkout.py /usr/local/lib/verify_checkout.py
COPY --chown=agent:agent logos-agent/workspace/screenshot.js /usr/local/lib/screenshot.js
# `logos-github`: how a session of a linked repository asks the runner to
# read or comment on GitHub, since the session holds no credential.
COPY logos-agent/workspace/logos_github.py /usr/local/lib/logos_github.py
RUN printf '#!/bin/sh\nexec python3 /usr/local/lib/logos_github.py "$@"\n' > /usr/local/bin/logos-github \
  && chmod 0755 /usr/local/bin/logos-github && chmod 0644 /usr/local/lib/logos_github.py

USER agent
WORKDIR /workspace

# HOME lives on the workspace volume because the root filesystem is
# read-only. The harness replaces it at the start of every session — a
# previous session is untrusted, and a credential-bearing session must not
# read the hooks, dotfiles, or poisoned caches it left — so nothing stored
# here is meant to survive a session.
#
# pre-commit's store goes with it, because a store is not read-only: it
# takes a lock file and writes to its little sqlite database on every run,
# and the seeded one at /opt/pre-commit is on the read-only root filesystem.
# The harness gives each session a store of its own holding a copy of the
# seeded database, whose rows point back at the hook environments in /opt —
# reading those is all pre-commit does with them once they are installed.
ENV HOME=/workspace/.home \
    PRE_COMMIT_HOME=/workspace/.home/pre-commit \
    NODE_PATH=/usr/local/lib/node_modules

ENTRYPOINT ["python3", "/usr/local/lib/run_session.py"]
