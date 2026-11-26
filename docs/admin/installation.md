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
development accounts. The local realm supplies their identities and the explicit development Liquibase context seeds their Logos roles.

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

## First Logos Admin

On a fresh production installation, have the designated operator sign in once. Verify their subject UUID directly in the configured Keycloak realm. Using a database administration session connected to this deployment, replace the example UUID with that exact subject and run:

```sql
UPDATE users SET role = 'logos_admin'
WHERE keycloak_id = '00000000-0000-0000-0000-000000000000'::uuid AND is_active
RETURNING id, username, keycloak_id, role;
```

Confirm the returned identity before using **Users** to assign subsequent roles. Keep the `dev-identities` Liquibase context confined to the development and screenshot stacks.

During upgrade, Logos keeps existing roles, team memberships, ownerships, keys and grants. Previously synchronized memberships become locally managed. Existing teams start with worldwide processing to preserve their available deployments. Existing request profiles continue to impose their own privacy thresholds. Review each team in **Teams → Settings** and confirm regional cloud endpoints before selecting EU processing. Keycloak sign-in and the optional account availability sync preserve local role and membership decisions.

## Persistent Data and Upgrades

The production stack keeps the following data:

| Storage | Type | Contents |
| --- | --- | --- |
| `postgres_data` | named volume | PostgreSQL data |
| `data_volume` | named volume | orchestrator working data (`/src/logos`) |
| `agent_artifacts` | named volume | agent session artifacts |
| `agent_state` (literal name `logos_agent_state`) | named volume | agent session state |
| `./letsencrypt` | bind mount | Traefik Let's Encrypt certificate state |

Before an upgrade, back up these volumes and the `./letsencrypt` directory.
Keycloak is external to the stack. Back up its realm export together with your
identity provider (for the realm format, see `keycloak/tum-realm.json` in this
repository). In the development stack, the Keycloak state is temporary. Pull
the necessary image tag and recreate the stack:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
```
