# AGENTS.md — logos-agent

Python 3.13 / FastAPI service that runs coding agents in isolated containers on serving capacity Logos is not otherwise using. See `README.md` for the architecture, isolation model, and API surface — the notes below are the non-obvious constraints.

## Non-obvious invariants

- **Isolation boundary**: the runner holds the Docker socket; session containers get no socket, no root, no capabilities, a read-only root filesystem, and memory/CPU/PID ceilings. That asymmetry is set in exactly one place: `app/docker_engine.py:create_session_container` — keep it that way.
- **Session network** is an *internal* bridge with no route off the host; the runner verifies this and refuses to start on a plain bridge. Session containers may only reach the model gateway.
- A session's model traffic goes through `logos-orchestrator`; sessions can be told to deploy their result to the dev environment and screenshot the pages they changed.

## Commands

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest            # asyncio_mode = "auto"
```

Inside the session image, compile changed Java/Angular services before finishing:

```bash
python3 /usr/local/lib/verify_checkout.py logos-webservice logos-ui
```

Omit services you did not change. This uses the image's offline dependency caches,
runs Maven `test-compile`, and runs the Angular production build and tests. Fix
compile/type failures before finishing. New dependencies absent from the image
cache are a failed check, not a pass. Lint alone does not verify compilation.

The runner repeats verification in a separate container with no credentials,
no network, and read-only workspace/artifact mounts. Never execute repository
build code in preparation, snapshot, or finalization helpers: they hold push
credentials. The runner stores the original checkout/branch SHAs outside the
workspace and binds the verification outcome to the exact Git tree. Missing
baseline proof stops publication. Failed verification publishes only a draft
with diagnostics and fails the session, preventing deployment.
Service selection covers the complete PR diff, including prior attempts, so a
documentation-only retry cannot skip a previously failing compiler check.

Commits retain the actual prepared parent. Later base-branch changes therefore
remain outside the agent diff; conflicting edits remain merge conflicts for
review. Do not attach an old working tree to a freshly fetched parent. Existing
PR branches must be prepared from their own current tip; an unexpected remote
branch movement stops publication.

Python style matches the repo: black 120 cols, isort (black profile), flake8.
