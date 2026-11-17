#!/usr/bin/env bash
# Regenerate (or verify) every lock file derived from the worker's
# requirements.txt. Each lock pins every package, direct or transitive, to an
# exact version with hashes; consumers install it with --no-deps and
# --require-hashes, so nothing is resolved at install time.
#
#   logos-workernode/requirements.lock
#       The vLLM image (Dockerfile). Resolved from the VLLM_PIP_SPEC default,
#       requirements.txt and flashinfer-python (kept explicit in case vLLM
#       drops the dependency) for the image's CPython 3.12 on x86_64 glibc.
#       The CUDA compiler wheels are pinned to the CUDA minor of the runtime
#       headers torch pins: torch constrains nvidia-cuda-runtime but not
#       nvidia-cuda-nvcc, so an unconstrained resolve floats nvcc to the newest
#       minor, and FlashInfer's bundled CCCL then rejects every JIT compile
#       with "CUDA compiler and CUDA toolkit headers are incompatible".
#
#   logos-workernode/requirements-standalone.lock
#       The worker without vLLM: the macOS worker venv (install-macos.sh) and
#       CI's worker venvs. Resolved from requirements.txt alone, universally
#       (macOS and Linux, CPython 3.12+).
#
#   e2e/requirements.lock
#       The e2e venv, which runs the suite and the worker in one interpreter.
#       Resolved from e2e/pyproject.toml (all extras and its build-system
#       requirements, so the editable install needs no isolated build env)
#       together with requirements.txt, universally for CPython 3.13+.
#
# Locks are compiled with the uv release the image installs with (the
# Dockerfile's uv stage), so their format does not depend on the local uv.
#
# Usage:
#   scripts/lock-requirements.sh            re-lock, keeping existing pins where possible
#   scripts/lock-requirements.sh --upgrade  re-lock to the newest allowed versions
#   scripts/lock-requirements.sh --check    fail if any lock is stale for its inputs
set -euo pipefail

WORKERNODE="$(cd "$(dirname "$0")/.." && pwd)"
E2E="$(cd "$WORKERNODE/../e2e" && pwd)"

MODE=lock
case "${1:-}" in
  "") ;;
  --upgrade) MODE=upgrade ;;
  --check) MODE=check ;;
  *) echo "usage: $0 [--upgrade|--check]" >&2; exit 2 ;;
esac

SPEC=$(sed -nE 's/^ARG VLLM_PIP_SPEC="([^"]+)"$/\1/p' "$WORKERNODE/Dockerfile")
if [ -z "$SPEC" ]; then
  echo "no ARG VLLM_PIP_SPEC default found in the Dockerfile" >&2
  exit 1
fi
UV_VERSION=$(sed -nE 's|^FROM ghcr\.io/astral-sh/uv:([0-9.]+) AS uv$|\1|p' "$WORKERNODE/Dockerfile")
if [ -z "$UV_VERSION" ]; then
  echo "no pinned uv stage found in the Dockerfile" >&2
  exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
STALE=0

# compile <lock path> <workdir> <uv pip compile args...>
# Runs inside <workdir>, where the inputs sit under fixed names, so the
# "# via" annotations in the lock do not depend on temporary paths. The
# existing lock is copied in first: its pins are preferences, so re-locking
# only moves what the inputs force (or everything, with --upgrade).
compile() {
  local lock="$1" dir="$2"
  shift 2
  local extra=()
  if [ "$MODE" = upgrade ]; then
    extra+=(--upgrade)
  fi
  if [ -f "$lock" ] && [ ! -f "$dir/out.lock" ]; then
    cp "$lock" "$dir/out.lock"
  fi
  (
    cd "$dir"
    uvx --quiet "uv@$UV_VERSION" pip compile --quiet --no-header --generate-hashes \
      ${extra[@]+"${extra[@]}"} "$@" -o out.lock
  )
}

# finish <lock path> <workdir>: write the result, or compare it in --check mode.
finish() {
  local lock="$1" dir="$2"
  if [ "$MODE" = check ]; then
    if [ -f "$lock" ] && diff -u "$lock" "$dir/out.lock"; then
      echo "$lock is up to date."
    else
      echo "$lock is stale." >&2
      STALE=1
    fi
  else
    cp "$dir/out.lock" "$lock"
    echo "Wrote $lock."
  fi
}

# ── vLLM image ────────────────────────────────────────────────────────────────
dir="$WORK/image"
mkdir -p "$dir"
printf '%s\nflashinfer-python\n' "$SPEC" > "$dir/image.in"
cp "$WORKERNODE/requirements.txt" "$dir/requirements.txt"
: > "$dir/constraints.txt"
image_args=(--python-version 3.12 --python-platform x86_64-manylinux_2_39
  --constraint constraints.txt image.in requirements.txt)
compile "$WORKERNODE/requirements.lock" "$dir" "${image_args[@]}"
runtime=$(sed -nE 's/^nvidia-cuda-runtime==([^ ]+).*/\1/p' "$dir/out.lock")
if [ -n "$runtime" ]; then
  cuda_mm=$(printf '%s' "$runtime" | cut -d. -f1-2)
  printf '%s\n' \
    "nvidia-cuda-nvcc==${cuda_mm}.*" \
    "nvidia-cuda-crt==${cuda_mm}.*" \
    "nvidia-nvvm==${cuda_mm}.*" > "$dir/constraints.txt"
  compile "$WORKERNODE/requirements.lock" "$dir" "${image_args[@]}"
fi
finish "$WORKERNODE/requirements.lock" "$dir"

# ── Standalone worker (macOS, CI) ─────────────────────────────────────────────
dir="$WORK/standalone"
mkdir -p "$dir"
cp "$WORKERNODE/requirements.txt" "$dir/requirements.txt"
compile "$WORKERNODE/requirements-standalone.lock" "$dir" \
  --universal --python-version 3.12 requirements.txt
finish "$WORKERNODE/requirements-standalone.lock" "$dir"

# ── e2e suite + worker ────────────────────────────────────────────────────────
dir="$WORK/e2e"
mkdir -p "$dir"
cp "$E2E/pyproject.toml" "$dir/pyproject.toml"
cp "$WORKERNODE/requirements.txt" "$dir/worker-requirements.txt"
python3 -c 'import sys, tomllib; print("\n".join(tomllib.load(open(sys.argv[1], "rb"))["build-system"]["requires"]))' \
  "$E2E/pyproject.toml" > "$dir/build-system.txt"
compile "$E2E/requirements.lock" "$dir" \
  --universal --python-version 3.13 --all-extras pyproject.toml worker-requirements.txt build-system.txt
finish "$E2E/requirements.lock" "$dir"

if [ "$STALE" -ne 0 ]; then
  echo "Run logos-workernode/scripts/lock-requirements.sh and commit the result." >&2
  exit 1
fi
