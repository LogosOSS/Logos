#!/usr/bin/env bash
# Build the E2E workflow's Java images once and transfer them without a registry.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
artifact_dir="${RUNNER_TEMP:?}/e2e-ci-images"
revision="${GITHUB_SHA:?}"
[[ "$revision" =~ ^[0-9a-f]{40}$ && "$(git -C "$repo" rev-parse HEAD)" == "$revision" ]] || {
    echo "E2E image revision does not match the checked-out commit" >&2
    exit 1
}

case "${1:-}" in
    build)
        cd "$repo"
        # A single Bake invocation deduplicates the shared Maven stages.
        COMPOSE_BAKE=true docker compose -p logos-e2e-ci --profile ui \
            -f e2e/harness/stack/docker-compose.e2e.yaml build migrate logos-webservice

        for component in migrate webservice; do
            service="$component"
            if [[ "$component" == webservice ]]; then
                service=logos-webservice
            fi
            image="logos-e2e-${component}:$revision"
            docker image tag "logos-e2e-ci-$service" "$image"
            (
                mkdir -p "$artifact_dir/$component"
                cd "$artifact_dir/$component"
                docker image save "$image" | gzip -1 > image.tar.gz
                sha256sum image.tar.gz > image.tar.gz.sha256
            )
        done
        ;;
    load)
        component="${2:-}"
        [[ "$component" == migrate || "$component" == webservice ]] || exit 2
        cd "$artifact_dir/$component"
        sha256sum --check image.tar.gz.sha256
        docker image load --input image.tar.gz
        docker image inspect "logos-e2e-${component}:$revision" --format '{{.Id}}'
        ;;
    *)
        echo "Usage: $0 build | load {migrate|webservice}" >&2
        exit 2
        ;;
esac
