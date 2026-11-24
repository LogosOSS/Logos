---
title: Self-Hosted Installation
---

# Self-Hosted Installation

This guide shows how to deploy the complete Logos stack with Docker Compose.
The stack contains the web UI, the API service, the orchestrator, PostgreSQL
and Traefik. The development stack also runs a local Keycloak. Production
needs an external identity provider. Configure it with the `KEYCLOAK_*`
variables in `.env`.

## Prerequisites

- Docker Engine with the Compose plugin
- A domain name and an SMTP/identity-provider configuration for production
- Python 3.13 and `uv`, only if you develop Logos outside containers
- A GPU worker node, if you serve local models

Clone the repository and go to the service directory:

```bash
git clone https://github.com/LogosOSS/Logos.git
cd Logos
cp .env.example .env
```

## Development Deployment

The development compose file builds all services locally. It includes a
development Keycloak realm:

```bash
docker compose -f docker-compose.dev.yaml up --build
```

The compose stack serves the API (Traefik at `http://localhost:18081`). It
does not serve the web UI. Also start the Angular dev server on the host:

```bash
cd logos-ui
npm ci
npm start
```

Then open `http://localhost:4200/` and sign in with one of the seeded
development accounts. `keycloak/tum-realm.json` defines these accounts and
their roles. Do not use them in a production deployment.

## Production Deployment

For production, use `docker-compose.yaml`. Logos publishes its images to
public GHCR at `ghcr.io/logososs`. The compose file uses this registry
by default. You do not need registry configuration or a login.
Set `LOGOS_DB_PASSWORD` and `LOGOS_REDIS_PASSWORD` in `.env` first.
Use separate random values.
Then start the services:

```bash
docker compose --env-file .env up -d
```

The `latest` tag follows `main`. Releases use version tags, and pull requests
use `pr-<number>`. Set `IMAGE_TAG` in `.env` to select a published tag.
PR images can be removed when the PR closes.
To use your own registry, set both variables in `.env`.
Log in if that registry requires credentials:

```bash
REGISTRY=<registry-host>/<namespace>
IMAGE_TAG=<published tag>
docker login <registry-host>         # host only, no path
```

To run images that you built yourself, build them from the Dockerfiles that
the build workflow uses. Tag them for a registry from which the host can pull,
for example a local registry:

```bash
# The build contexts below are relative to the repository root. Go there
# first (the working directory of this guide is Logos):
cd ..
REGISTRY=localhost:5000
IMAGE_TAG=latest
docker build -t "$REGISTRY/logos-orchestrator:$IMAGE_TAG" -f logos-orchestrator/Dockerfile .
docker build -t "$REGISTRY/logos-webservice:$IMAGE_TAG" logos-webservice
docker build -t "$REGISTRY/logos-ui:$IMAGE_TAG" logos-ui
docker build -t "$REGISTRY/logos-db:$IMAGE_TAG" db
docker build -t "$REGISTRY/logos-agent:$IMAGE_TAG" -f logos-agent/Dockerfile .
docker build -t "$REGISTRY/logos-agent-gateway:$IMAGE_TAG" -f agent-gateway/Dockerfile .
docker build -t "$REGISTRY/logos-agent-workspace:$IMAGE_TAG" -f logos-agent/workspace/Dockerfile .
docker build -t "$REGISTRY/logos-rate-gateway:$IMAGE_TAG" -f rate-limit-gateway/Dockerfile .
```

The full stack needs all eight images. `logos-rate-gateway` is the only router
on the public entrypoints. Without it, the stack serves no public traffic. If
the `logos-agent-workspace` image is absent, the agent runner refuses to start
a session.

Set the same `REGISTRY` and `IMAGE_TAG` in `.env`, then start the stack. Build
the worker node image on the GPU host instead (see the worker node guide):

```bash
cd logos
docker compose --env-file .env up -d
```

Set real values for `LOGOS_DOMAIN`, `ACME_EMAIL` and
`LOGOS_CORS_ALLOWED_ORIGINS`. Set strong values for `LOGOS_INTERNAL_SECRET`
and `PROMETHEUS_API_KEY`. Make sure that ports 80, 443 and (if necessary) 8080
of the host are available. If you configure `ACME_EMAIL`, Traefik gets a
certificate through Let's Encrypt.

After startup, verify the UI at `https://<your-domain>/`. Verify the API
documentation at `https://<your-domain>/docs`.

## Persistent Data and Upgrades

The production stack keeps the following data:

| Storage | Type | Contents |
| --- | --- | --- |
| `postgres_data` | named volume | PostgreSQL data |
| `data_volume` | named volume | orchestrator working data (`/src/logos`) |
| `agent_artifacts` | named volume | agent session artifacts |
| `agent_state` (literal name `logos_agent_state`) | named volume | agent session state |
| `./letsencrypt` | bind mount | Traefik Let's Encrypt certificate state |

For a PostgreSQL 17 installation, complete the [database upgrade](#upgrade-postgresql-17-to-18) before the general upgrade steps.

Before an upgrade, back up these volumes and the `./letsencrypt` directory.
Keycloak is external to the stack. Back up its realm export together with your
identity provider (for the realm format, see `keycloak/tum-realm.json` in this
repository). In the development stack, the Keycloak state is temporary. Pull
the necessary image tag and recreate the stack:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

## Upgrade PostgreSQL 17 to 18

PostgreSQL 18 uses `/var/lib/postgresql/18/docker` inside the volume mounted at `/var/lib/postgresql`. An existing PostgreSQL 17 volume requires a major-version migration.

1. Schedule a maintenance window. Record the current application tag, database image and physical volume name. Verify a backup on a separate system.
2. Stop the application services and other writers. Suspend scheduled database jobs for the full maintenance window. Keep the PostgreSQL 17 server available to the backup client.
3. Use the PostgreSQL 18 `pg_dumpall` client to export the cluster, including databases, roles and grants. Connect with a source superuser. Restrict access to the dump because it contains role password hashes. Check the command exit status and available target disk space. Record the source database sizes, application row counts, Liquibase changesets and scheduled jobs. Stop the PostgreSQL 17 server after the export.
4. Start the Logos PostgreSQL 18 image on a separate, empty volume and an isolated network. Mount the volume at `/var/lib/postgresql`. Use `POSTGRES_DB=postgres` and a temporary `POSTGRES_USER` name that does not exist in the source cluster. Set a temporary bootstrap password. Start the server with `shared_preload_libraries=pg_cron`, `cron.database_name=logosdb` and `cron.launch_active_jobs=off`. Install any other source extensions and prepare any custom tablespaces before the restore.
5. Restore the complete dump with `psql -X -v ON_ERROR_STOP=1` as the temporary superuser, initially connected to `postgres`. Verify the original application login, roles, grants, sequences, application records, scheduled jobs and Liquibase history. Disable login for the temporary superuser from the restored administrative account. Stop the isolated server after validation.
6. Set `LOGOS_DB_VOLUME_NAME` to the exact restored volume name in the core deployment environment. For GitHub Actions, set the variable in the environment used by the core deploy. Check `docker volume inspect` and the resolved `docker compose config` before starting the service. The normal database service enables scheduled jobs at startup. Confirm the data directory and restored records before you reopen application traffic.

Keep the original volume and image until the upgrade is accepted. Before accepting new writes, you can restore service with the previous Compose version, image and volume setting. After writes resume, a switch to the old volume would lose those writes. Reconcile or recover them before a rollback.

See the [PostgreSQL cluster upgrade procedure](https://www.postgresql.org/docs/18/upgrading.html) and the [pg_dumpall reference](https://www.postgresql.org/docs/18/app-pgdumpall.html).
