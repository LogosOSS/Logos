#!/usr/bin/env bash
set -euo pipefail

: "${E2E_SIM_MIGRATE_IMAGE:?Set the simulation migration image tag}"
: "${E2E_SIM_SOURCE_SHA:?Set the source revision}"

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

# The job timeout bounds individual builds; only a recognised download failure
# can start another attempt. BuildKit keeps completed layers on this runner.
transient_download='(download|transfer|fetch|resolve|repository|https?://).*(429|500|502|503|504|timed out|connection reset|temporary failure)|429 Too Many Requests'
for attempt in 1 2 3; do
    if docker buildx build --load --progress=plain --target e2e-migrate \
        --tag "$E2E_SIM_MIGRATE_IMAGE" \
        --label "org.opencontainers.image.revision=$E2E_SIM_SOURCE_SHA" \
        logos-webservice 2>&1 | tee "$log"; then
        exit 0
    else
        status=$?
    fi
    if [[ "$attempt" == 3 ]] || ! grep -Eiq "$transient_download" "$log"; then
        exit "$status"
    fi
    delay=$((10 * 2 ** (attempt - 1)))
    printf 'Migration image download failed (attempt %s/3); retrying in %ss\n' "$attempt" "$delay" >&2
    sleep "$delay"
done
