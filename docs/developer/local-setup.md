---
title: Local Development
---

# Local Development

All blocks below start from the repository root (`Logos/`). Each block
sets its own working directory. You can run the blocks in any order from a
new checkout.

## Development Stack

```bash
docker compose -f docker-compose.dev.yaml up --build
```

The compose stack serves the API (Traefik at `http://localhost:18081`). It
does not serve the web UI. Also start the Angular dev server on the host (see
the Angular UI block below). Then open `http://localhost:4200/`.

| Compose file | Purpose |
| --- | --- |
| `docker-compose.yaml` | Production. It pulls published images from GHCR. |
| `docker-compose.dev.yaml` | Development. It builds all images from the source. |
| `docker-compose.screenshots.yaml` | A separate stack for documentation screenshots. See `docs/AGENTS.md`. |

## Accounts

Select **Sign in** on `http://localhost:4200/`. The local Keycloak realm
(`keycloak/tum-realm.json`) contains these accounts. The password of each
account is `password`.

| Username | Role | Use |
| --- | --- | --- |
| `tobias.wasner` | `logos_admin` | All pages, including providers, team privacy and workers |
| `alexandra.szuminska` | `app_admin` | Teams, users and models |
| `henriette.huhn` | `app_developer` | Models through a team |
| `blub.fisch` | `app_developer` | Models through a team |
| `fridoline.fuchs` | `app_developer` | Models through a team |
| `pech.vogel` | `app_developer` | The no-team and no-access screens |

To add an account, edit `keycloak/tum-realm.json`. Then restart the
`keycloak` container.

## Give a Developer a Team

Sign in as `tobias.wasner`, open **Teams → New Team**, choose its privacy setting and add an owner. In the team’s **Members** tab, choose **Import CSV**. Map first name, last name and email, then import the selected rows. Choose **Add Member** for one existing account. Logos stores membership, ownership and global roles. Use **Users** to change roles. Signing in again preserves these choices.

## Orchestrator (Python)

The orchestrator is the main Python service. It is an installable package in
`logos-orchestrator/`. Use Python 3.13 and `uv`:

It installs the exact versions from `uv.lock`:

```bash
cd logos-orchestrator
uv sync --locked --python 3.13
```

The `pyproject.toml` in the repository root only configures the formatters.
It is not the orchestrator package. Thus you can ignore the warning about
its missing `[project]` table.

In PyCharm, use `logos-orchestrator/.venv/bin/python` as the interpreter. If
PyCharm shows **Package requirements are not satisfied**, select
**Sync project**. PyCharm then installs the packages with `uv`. You do not
need a `requirements.txt` file.

## Angular UI

You can develop the UI separately:

```bash
cd logos-ui
npm ci
npm start
```

On Linux, `npm` can show `EACCES` permission errors. If this occurs, read the
[npm documentation](https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally).

## Pre-commit Hooks

The pre-commit hooks do the same checks as the lint job in CI
(`.github/workflows/logos_lint.yml`). Every hook runs a tool from `PATH`, and
`tools/pre-commit/requirements.lock` pins pre-commit and all of these tools to
exact versions with hashes. Install them into a venv and the hooks once in
each clone:

```bash
uv venv .venv-pre-commit --python 3.13
uv pip install --python .venv-pre-commit/bin/python --no-deps --require-hashes \
  -r tools/pre-commit/requirements.lock
source .venv-pre-commit/bin/activate
pre-commit install
```

Activate that venv before you commit, so Git's hook finds the tools.

Git then runs the hooks before each commit. Before you submit changes, run
all hooks on all files:

```bash
pre-commit run --config .pre-commit-config.yaml --all-files
```

| Hook | Purpose |
| --- | --- |
| `pre-commit-hooks` | YAML and TOML syntax, large files, merge conflicts, end of file, trailing spaces |
| `autoflake` | Removes unused imports and variables |
| `isort` | Sorts the imports (`profile=black`, 120 columns) |
| `black` | Formats Python (`line-length = 120`, `target = py313`) |
| `flake8` | Checks against `.flake8` (E203 and W503 are off for `black`) |

The lint job fails if a hook changes a file or finds a problem. Correct the
problem. Do not use `git commit --no-verify`, because the CI job still fails.
