<div align="center">

<img src="docs/static/img/logos-logo.svg" alt="Logos logo" width="96" height="96">

# Logos

**LLM orchestration and management**

One OpenAI-compatible gateway in front of self-hosted GPU workers and cloud providers,
with policy-based routing, scheduling, usage logging, billing and monitoring.

[![Test](https://github.com/LogosOSS/Logos/actions/workflows/logos_test.yml/badge.svg?branch=main)](https://github.com/LogosOSS/Logos/actions/workflows/logos_test.yml)
[![Lint](https://github.com/LogosOSS/Logos/actions/workflows/logos_lint.yml/badge.svg?branch=main)](https://github.com/LogosOSS/Logos/actions/workflows/logos_lint.yml)
[![E2E](https://github.com/LogosOSS/Logos/actions/workflows/logos_e2e.yml/badge.svg?branch=main)](https://github.com/LogosOSS/Logos/actions/workflows/logos_e2e.yml)
[![Documentation](https://github.com/LogosOSS/Logos/actions/workflows/docs.yml/badge.svg?branch=main)](https://logososs.github.io/Logos/)
[![License: MIT](https://img.shields.io/badge/license-MIT-7c3aed.svg)](LICENSE)

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=LogosOSS_Logos&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=LogosOSS_Logos)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=LogosOSS_Logos&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=LogosOSS_Logos)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=LogosOSS_Logos&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=LogosOSS_Logos)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=LogosOSS_Logos&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=LogosOSS_Logos)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=LogosOSS_Logos&metric=coverage)](https://sonarcloud.io/summary/new_code?id=LogosOSS_Logos)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=LogosOSS_Logos&metric=duplicated_lines_density)](https://sonarcloud.io/summary/new_code?id=LogosOSS_Logos)

[Documentation](https://logososs.github.io/Logos/) ·
[User Guide](https://logososs.github.io/Logos/user/getting-started) ·
[Administrator Guide](https://logososs.github.io/Logos/admin/installation) ·
[Developer Guide](https://logososs.github.io/Logos/developer/system-design)

<a href="https://logososs.github.io/Logos/">
  <img src="docs/static/video/logos-trailer-poster.jpg" alt="Watch the 60 s Logos trailer on the documentation homepage" width="720">
</a>

<sub>▶ Watch the 60 s trailer on the <a href="https://logososs.github.io/Logos/">documentation homepage</a>.</sub>

</div>

## Why Logos

- **One API for every model.** Clients use the OpenAI APIs they already know — Chat Completions,
  Responses, audio, files and batches — against one endpoint, whether the model runs on your own
  GPUs or at Azure or OpenAI.
- **Policies decide where a request goes.** Routing weighs privacy, priority, cost and quality, so
  sensitive data stays on self-hosted workers and cheap requests do not land on expensive models.
- **Your GPUs, fully used.** The orchestrator schedules requests across vLLM lanes on worker nodes,
  wakes and sleeps lanes on demand and calibrates VRAM profiles automatically.
- **Teams, keys and budgets.** Every request belongs to a team and an API key, with per-model
  permissions, monthly budgets and rate limits.
- **Everything is measured.** Requests, tokens, cold starts and cloud cost are logged and shown live;
  nightly benchmarks keep latency and throughput numbers current.
- **Spare capacity does work.** Coding agents run in isolated containers on capacity that interactive
  traffic leaves unused.

## Quick links

| Topic | Where to find it |
| --- | --- |
| Documentation | https://logososs.github.io/Logos/ |
| Call the API | [User Guide → API usage](https://logososs.github.io/Logos/user/api-usage) |
| Install and operate Logos | [Administrator Guide](https://logososs.github.io/Logos/admin/installation) |
| Add a GPU worker node | [Worker node](https://logososs.github.io/Logos/admin/worker-node) |
| System design and architecture | [System design](https://logososs.github.io/Logos/developer/system-design) · [Architecture reference](https://logososs.github.io/Logos/developer/architecture) |
| What each role can do | [Logos Admin](https://logososs.github.io/Logos/roles/logos-admin) · [App Admin](https://logososs.github.io/Logos/roles/app-admin) · [App Developer](https://logososs.github.io/Logos/roles/app-developer) |
| Local development setup | [Developer Guide → Local setup](https://logososs.github.io/Logos/developer/local-setup) |
| Security policy | [SECURITY.md](SECURITY.md) |
| Code of conduct | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |
| Report a bug or request a feature | [Issues](https://github.com/LogosOSS/Logos/issues/new/choose) |

## Try it

Run the full stack locally from the repository root, then start the web application:

```bash
docker compose -f docker-compose.dev.yaml up --build
cd logos-ui && npm ci && ng serve
```

Open [http://localhost:4200](http://localhost:4200) and select **Sign in**. The local Keycloak realm
(`keycloak/tum-realm.json`) seeds these accounts, all with the password `password`:

| Username | Role | Use it for |
| --- | --- | --- |
| `tobias.wasner` | `logos_admin` | Everything, including providers, policies and workers |
| `alexandra.szuminska` | `app_admin` | Teams, users and models |
| `henriette.huhn`, `blub.fisch`, `fridoline.fuchs` | `app_developer` | Using models through a team |
| `pech.vogel` | none | The no-team and no-access screens |

A fresh stack has no teams, so `app_developer` accounts see nothing until they join one.
[Local development](https://logososs.github.io/Logos/developer/local-setup) shows 3 ways to give them a team.

The interactive API reference is served at `/docs` (Swagger). A `GET /v1` returns 404 by design.

## Key capabilities

| Area | What it does |
| --- | --- |
| [OpenAI-compatible API](https://logososs.github.io/Logos/user/api-usage) | `/v1/chat/completions`, `/v1/responses`, embeddings and models, streaming included; one API key per team member or application |
| [Audio](https://logososs.github.io/Logos/user/api-usage#audio-transcription-and-translation) | `/v1/audio/transcriptions` and `/v1/audio/translations` against cloud Whisper deployments or compatible worker backends |
| [Batch processing](https://logososs.github.io/Logos/batch-processing) | The OpenAI Batch API (`/v1/files`, `/v1/batches`); runs at the provider's batch rate where possible, otherwise inside Logos at the lowest queue priority. The **Batches** page does the same without a script |
| Async jobs | `POST /jobs/v1/chat/completions` returns `202` with a `Location` header; poll `GET /jobs/{job_id}` |
| [Decision models](https://logososs.github.io/Logos/decision-models) | `POST /v1/systemone` answers typed questions (yes/no, choice, score) with calibrated probabilities |
| Policies and routing | Select a model and provider per request by privacy, priority, cost and quality |
| [Scheduling and capacity](https://logososs.github.io/Logos/admin/operations) | Queues requests across vLLM lanes; the capacity planner sleeps idle lanes, wakes lanes on demand and tunes GPU memory utilization |
| [Worker nodes](https://logososs.github.io/Logos/admin/worker-node) | GPU machines dial out to the core node over WebSocket, so they need no inbound ports; VRAM profiles are calibrated automatically and stored centrally |
| [Benchmarks](https://logososs.github.io/Logos/admin/model-benchmarks) | Manual benchmarks with saved request datasets, plus a [nightly rotation](https://logososs.github.io/Logos/admin/nightly-benchmarks) that compares batching parameters per model and worker |
| Teams, keys and budgets | Teams with members, application keys, per-model permissions, monthly budgets and rate limits |
| Statistics and billing | Live requests, tokens, cold starts, lane health and cloud cost; spend per team over time |
| [Coding agents](logos-agent/README.md) | Runs coding agents in isolated containers on spare serving capacity; **AI Tools** connects Claude Code or OpenCode in guided steps |
| [Context windows](https://logososs.github.io/Logos/context-windows) | The window follows the KV cache of the serving lane; `GET /v1/models` reports what each model can take |
| [Passkey login](https://logososs.github.io/Logos/passkey-login) | WebAuthn passkey sign-in directly in the web application |

## Architecture

Every client request enters through one gateway (`logos-rate-gateway`, then `traefik`), which
load-balances it onto one of the `logos-webservice` replicas, the only component that runs ×N. The
web service authenticates the request and checks the budget, then calls the cloud provider directly
or hands local and mixed requests to `logos-orchestrator`, which schedules them onto GPU worker
nodes. Workers dial out to the orchestrator over WebSocket, so they need no inbound ports.

<p align="center">
  <img src="docs/static/img/system-design/top-level.svg" alt="UML component diagram of Logos: every client request enters through the gateway to the logos-webservice replicas" width="820">
</p>

The [system design](https://logososs.github.io/Logos/developer/system-design) covers the deployment
view, the data model and the request flow. The
[request lifecycle reference](logos-orchestrator/src/logos/pipeline/README.md) describes the
classification, scheduling, context-resolution, forwarding and completion boundaries.

## Repository layout

| Directory | Stack | Contents |
| --- | --- | --- |
| [`logos-orchestrator/`](logos-orchestrator) | Python 3.13, FastAPI, uv | Core proxy: authentication, classification, scheduling, provider routing, request logging |
| [`logos-webservice/`](logos-webservice) | Java 25, Spring Boot | Management REST API and public inference gateway (`/v1`, `/openai`, `/jobs`); owns the PostgreSQL schema (Liquibase) |
| [`logos-ui/`](logos-ui) | Angular | Web application for teams, keys, models, statistics and billing |
| [`logos-workernode/`](logos-workernode) | Python | GPU worker-node control plane: vLLM lane lifecycle, calibration, WebSocket bridge |
| [`logos-node-manager/`](logos-node-manager) | Python | Deploys GPU worker nodes |
| [`logos-agent/`](logos-agent) | Python, FastAPI | Runs coding agents in isolated containers |
| [`e2e/`](e2e) | pytest, Playwright | GPU-less end-to-end suite |
| [`docs/`](docs) | Docusaurus | User, administrator and developer documentation |
| `agent-gateway/`, `rate-limit-gateway/` | nginx | Edge proxies around the agent runner and the orchestrator |
| `keycloak/` | — | Development realm |
| `db/` | — | PostgreSQL 17 with pg_cron |
| `benchmarks/`, `scripts/` | Python, bash | Scheduler benchmarks and operations scripts |

| Compose file | Purpose |
| --- | --- |
| `docker-compose.yaml` | Production: pulls pre-built images from Harbor |
| `docker-compose.dev.yaml` | Development: builds all images from source |
| `docker-compose.screenshots.yaml` | Isolated stack for documentation screenshots (see [`docs/AGENTS.md`](docs/AGENTS.md)) |

## Development

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Docker with Docker Compose, Node.js
for `logos-ui/` and `docs/`, and [Artemis developer access](https://request.aet.cit.tum.de/) to
push to this repository.

```bash
(cd logos-orchestrator && uv sync --locked --python 3.13) # orchestrator dependencies
pre-commit install                                         # from the repository root
pre-commit run --all-files                                 # the same checks as the lint CI job
```

[Local development](https://logososs.github.io/Logos/developer/local-setup) covers the dev stack, the seeded accounts, the
PyCharm setup and every pre-commit hook. The [TUM test instance](https://logososs.github.io/Logos/developer/test-instance)
page covers SSH and database access to `logos-test.aet.cit.tum.de`.

### Conventions

- **Pull request titles** are short, descriptive and start with a capital letter, for example
  `Add team management endpoints`.
- Every pull request that changes the web application includes desktop and mobile screenshots.
- More rules for contributors and coding agents are in [`AGENTS.md`](AGENTS.md).

## License

Logos is released under the [MIT License](LICENSE) by TUM Applied Education Technologies.
For the attribution and provenance of the LiteLLM model catalog data, see
[litellm-model-catalog.NOTICE](logos-webservice/src/main/resources/litellm-model-catalog.NOTICE).
