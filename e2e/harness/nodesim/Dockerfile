# Simulated worker node: the real logos_worker_node, fake hardware underneath.
#
# The production image is a CUDA base with vLLM compiled in — several GB, and it
# needs an NVIDIA container runtime to start. This image swaps that bottom layer
# for the GPU simulator: same worker code, same config handling, same bridge
# protocol, but `nvidia-smi` and `vllm` on PATH are the shims from
# harness/gpusim/bin.
#
# That keeps the whole inter-node tier runnable on an ordinary CI runner, and
# keeps the thing being tested honest — no worker module is stubbed or patched.
#
# Build context is the `logos/` directory:
#   docker build -f e2e/harness/nodesim/Dockerfile logos/

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# procps/psmisc: the worker walks the process tree (pgrep) when accounting for
# host RAM across vLLM TP workers, and the watchdog's diagnostics dump shells
# out for system state. Leaving them out would test failure paths nobody asked
# about.
# apt installs exact versions from a fixed Debian archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together.
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i -E \
      -e "s#https?://deb\.debian\.org/debian-security/?#https://snapshot.debian.org/archive/debian-security/${APT_SNAPSHOT}/#g" \
      -e "s#https?://deb\.debian\.org/debian/?#https://snapshot.debian.org/archive/debian/${APT_SNAPSHOT}/#g" \
      /etc/apt/sources.list.d/debian.sources \
  && ! grep -q deb.debian.org /etc/apt/sources.list.d/debian.sources

RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3 update \
  && apt-get install -y --no-install-recommends \
       procps=2:4.0.4-9 psmisc=23.7-2 curl=8.14.1-2+deb13u7 \
  && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

# The worker's standalone lock: exact versions with hashes, without vLLM and
# its CUDA stack (the simulator provides the `vllm` binary, and the real one
# would pull ~5 GB of wheels that no simulated lane ever loads).
COPY logos-workernode/requirements-standalone.lock /tmp/requirements.lock
RUN uv pip install --system --no-cache --no-deps --require-hashes -r /tmp/requirements.lock \
 && uv pip check --system \
 && rm /tmp/requirements.lock

COPY logos-workernode/logos_worker_node/ /app/logos_worker_node/
COPY e2e/harness/ /app/harness/

# The shims resolve their own imports relative to this layout, so `harness` must
# sit where `parents[3]` from harness/gpusim/bin/<tool> lands.
ENV PATH="/app/harness/gpusim/bin:${PATH}" \
    PYTHONPATH=/app \
    LOGOS_GPUSIM_STATE=/app/state/gpusim.json \
    LOGOS_GPUSIM_VLLM_SCRIPT=/app/state/vllm_script.json \
    LOGOS_WORKER_NODE_CONFIG=/app/config.yml \
    LOGOS_STATE_DIR=/app/data

COPY e2e/harness/nodesim/entrypoint.py /app/entrypoint.py

EXPOSE 80
CMD ["python", "/app/entrypoint.py"]
