---
title: Operations and Troubleshooting
---

# Operations and Troubleshooting

To read the service logs, use these commands:

```bash
docker compose --env-file .env logs -f logos-orchestrator
docker compose --env-file .env logs -f logos-webservice
```

The UI, the Swagger API, and the completion API use the same HTTPS endpoint.
A `404` from `GET /v1` is normal. Use `/docs` for Swagger. If browser POST or
WebSocket requests fail but GET requests work, check
`LOGOS_CORS_ALLOWED_ORIGINS`.

If a worker cannot connect, make sure of these items:

- `LOGOS_URL` uses `https://`.
- The shared key is the same as the key of the registered provider.
- The worker can resolve the name of the orchestrator and can reach it.

If a model fails, check the provider permissions and the available lanes.
Also check the runtime status of the worker. Use the Logos UI, or use the
`POST /logosdb/providers/logosnode/status` endpoint of the orchestrator (see
the worker node guide).

The capacity planner runs in the orchestrator every 30 s. It puts idle lanes
to sleep, wakes lanes when requests arrive, and tunes the vLLM GPU memory
utilization. It is on by default (`LOGOS_CAPACITY_PLANNER_ENABLED: "true"`).

The worker nodes measure the VRAM profile of each model when it is loaded and
when it sleeps. Logos stores the profiles in its database. It sends the
profiles to each worker when the worker starts. The workers keep no profiles
on disk and need no configuration for this.

Disable the capacity planner only to find the cause of a scheduling problem.
Set the variable in the `environment` of `logos-orchestrator`:

```yaml
LOGOS_CAPACITY_PLANNER_ENABLED: "false"
```
