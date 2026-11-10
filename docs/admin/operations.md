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

## Load answers for answer reuse

Answer reuse sends only answers that are in the answer cache. A new
installation has no stored answers. Load the answers for each reuse context
with the seed command:

1. Write a JSON Lines file with one question on each line. Each line has the
   fields `question`, `intent` and `answer`.
2. Find the owner of the API keys that send the requests. Use the team ID if
   the API keys are in a team. Use the API key ID if the API key is not in a
   team.
3. Run the command in the orchestrator container. Use the same `--context`
   value as the `logos-context` header of the requests.

```json
{"question": "Where is my package?", "intent": "track_order", "answer": "Track your package here: {{Order Tracking URL}}."}
```

```bash
docker exec -i logos-orchestrator python -m logos.answer_reuse.seed --team-id 3 --context shop-a < answers.jsonl
```

For an API key without a team, use `--key-id` and the API key ID. If the API
key is in a team, the command stops with an error. The requests of that API
key use the answers of the team.

The command shows the number of stored answers. If the context has a question
already, the command does not store it again. Thus, you can load the same file
again. The orchestrator uses new answers after a maximum of 30 s.

Logos sends a stored answer only if the 3 most similar stored questions have
the same intent. Thus, load a minimum of 3 questions for each intent.
