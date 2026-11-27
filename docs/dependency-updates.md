# Dependency updates and exact pins

Dependencies should receive reviewable update PRs without changing the bytes
used by an existing revision. Keep direct npm versions exact, commit the
lockfile, and use `npm ci` in builds. Keep Python services on their exact
requirements and locked `uv` environments or platform-specific hash locks.
Do not auto-merge dependency updates.

## Coverage

| Inputs | Update path |
| --- | --- |
| UI, documentation, Agent Workspace npm manifests | Dependabot npm updates the exact requirement and its committed lockfile. The checked-in documentation shim is reviewed as source, not as a registry package. |
| Agent, node manager and orchestrator Python services | Dependabot uv updates the manifest and lockfile. The root Python configuration has lint settings, not an installable dependency manifest. |
| Worker, end-to-end, benchmarks and pre-commit Python inputs | Dependabot pip proposes source pins. This repository's platform and hash lock generator is authoritative; CI rejects a stale lock and provides a patch. The two vLLM workflows coordinate coupled worker and accelerator updates. |
| Webservice Maven | Dependabot Maven follows the POM. |
| GitHub-hosted workflows and composite actions | Dependabot GitHub Actions updates action refs; keep immutable commit refs. |
| External Docker and Compose images | Dependabot Docker and Docker Compose. Logos's own release images are selected by the deployment, not by dependency updates. |
| OS repository snapshots/package pins, binary release checksums and CI tool setup versions | The weekly platform-version report identifies candidates. Dependabot does not interpret arbitrary shell commands, Docker build arguments or package-manager snapshots. Update versions, verified checksums and the affected platform lock together. |

The Angular compiler supports TypeScript 6.0 rather than 7. The documentation
build is separate. Keep compatibility overrides on versions supported by their
consumers; be deliberate about a breaking upgrade instead of using the npm
latest tag in a build.
