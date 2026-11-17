#!/usr/bin/env bash
# Regenerate (or verify) requirements.lock, the hash-pinned package set the
# vLLM worker image installs. The lock is resolved from:
#   - the VLLM_PIP_SPEC default in the Dockerfile (vLLM plus its extras),
#   - requirements.txt (the worker's own direct dependencies),
#   - flashinfer-python (kept explicit in case vLLM drops the dependency),
# for the image's interpreter and platform (CPython 3.12, x86_64 glibc).
#
# The CUDA compiler wheels are pinned to the CUDA minor of the runtime headers
# that torch pins: torch constrains nvidia-cuda-runtime but not
# nvidia-cuda-nvcc, so an unconstrained resolve floats nvcc to the newest
# minor, and FlashInfer's bundled CCCL then rejects every JIT compile with
# "CUDA compiler and CUDA toolkit headers are incompatible".
#
# The lock is compiled with the same uv release the image installs it with
# (the Dockerfile's uv stage), so its format does not depend on the local uv.
#
# Usage:
#   scripts/lock-vllm-image.sh            re-lock, keeping existing pins where possible
#   scripts/lock-vllm-image.sh --upgrade  re-lock to the newest allowed versions
#   scripts/lock-vllm-image.sh --check    fail if the lock is stale for its inputs
set -euo pipefail

cd "$(dirname "$0")/.."

MODE=lock
case "${1:-}" in
  "") ;;
  --upgrade) MODE=upgrade ;;
  --check) MODE=check ;;
  *) echo "usage: $0 [--upgrade|--check]" >&2; exit 2 ;;
esac

LOCK=requirements.lock
SPEC=$(sed -nE 's/^ARG VLLM_PIP_SPEC="([^"]+)"$/\1/p' Dockerfile)
if [ -z "$SPEC" ]; then
  echo "no ARG VLLM_PIP_SPEC default found in Dockerfile" >&2
  exit 1
fi

UV_VERSION=$(sed -nE 's|^FROM ghcr\.io/astral-sh/uv:([0-9.]+) AS uv$|\1|p' Dockerfile)
if [ -z "$UV_VERSION" ]; then
  echo "no pinned uv stage found in Dockerfile" >&2
  exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Inputs are compiled from inside $WORK under fixed names so the "# via"
# annotations in the lock do not depend on the temporary directory.
printf '%s\nflashinfer-python\n' "$SPEC" > "$WORK/image.in"
cp requirements.txt "$WORK/requirements.txt"
: > "$WORK/constraints.txt"

# Existing pins are preferences: re-locking only moves what the inputs force.
if [ -f "$LOCK" ]; then
  cp "$LOCK" "$WORK/$LOCK"
fi

compile() {
  local extra=()
  if [ "$MODE" = upgrade ]; then
    extra+=(--upgrade)
  fi
  (
    cd "$WORK"
    uvx --quiet "uv@$UV_VERSION" pip compile --quiet --no-header --generate-hashes \
      --python-version 3.12 --python-platform x86_64-manylinux_2_39 \
      --constraint constraints.txt \
      ${extra[@]+"${extra[@]}"} \
      image.in requirements.txt -o "$LOCK"
  )
}

pinned() {
  sed -nE "s/^$1==([^ ]+).*/\1/p" "$WORK/$LOCK"
}

compile
runtime=$(pinned nvidia-cuda-runtime)
if [ -n "$runtime" ]; then
  cuda_mm=$(printf '%s' "$runtime" | cut -d. -f1-2)
  printf '%s\n' \
    "nvidia-cuda-nvcc==${cuda_mm}.*" \
    "nvidia-cuda-crt==${cuda_mm}.*" \
    "nvidia-nvvm==${cuda_mm}.*" > "$WORK/constraints.txt"
  compile
fi

if [ "$MODE" = check ]; then
  if ! diff -u "$LOCK" "$WORK/$LOCK"; then
    echo "$LOCK is stale: run logos-workernode/scripts/lock-vllm-image.sh and commit the result." >&2
    exit 1
  fi
  echo "$LOCK is up to date."
else
  cp "$WORK/$LOCK" "$LOCK"
  echo "Wrote $LOCK (vLLM spec: $SPEC, uv $UV_VERSION)."
fi
