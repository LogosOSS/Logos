# AGENTS.md - logos-node-manager

Logos-node-manager handling GPU worker node redeployments. In `../.github/workflows/logos_deploy-dev.yml` and `logos_deploy-prod.yml`, `max-parallel: 1` runs one host at a time. For a worker without `drain: true`, the job lasts until the new container is verified, so the other worker nodes stay available to the orchestrator. A worker with `drain: true` (which needs `node-manager: true`) ends its job once the image is pulled, and the replacement continues on the host. For those workers `max-parallel: 1` serializes nothing; the orchestrator cordons one worker node at a time instead. A calibration or benchmark on another node is limited separately (`LOGOS_MAINTENANCE_MAX_NODES`).

Sources of truth:
- `README.md`

If the code does not align with what is written in `README.md`, check if there is a test pinning the code behaviour, if so, the `README.md` is stale. Update it in the same change with the correct behaviour. If no test exists, or you suspect the code mismatch is a bug, report it, and ask how to proceed.

## Notes to agents

- Do not flip the `node-manager` or the `drain` flag in the `logos_deploy-dev.yml` and `logos_deploy-prod.yml` files by yourself.
- You are NOT allowed to dispatch deploys or touch real hosts.
- Every step must be safe to repeat, because a restart can re-enter any phase.
- ALWAYS save the record before an irreversible action.
- Only clean up, if the record is finished.
- Keep the workers without `drain: true` before the ones with it in the matrix, or give all of them the flag (see the comment above `matrix:`). This applies to a new matrix entry too. Do not change `max-parallel: 1`.

## Tests

Tests are found in the `tests` folder. A special test is run by using real Docker, it needs a running Docker engine, the compose plugin, and a network connection. To run it, run the `run-smoke.sh` in `tests/smoke/`. The tests are safe to run, `tests/smoke/run-smoke.sh` creates and deletes only `nm-smoke*` things, and `KEEP=1` leaves them behind, they are not deleted automatically.
