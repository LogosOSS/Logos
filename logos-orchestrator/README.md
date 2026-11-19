# logos-orchestrator

Python FastAPI service — LLM gateway, proxy, scheduling, and capacity management.

See the root [README](../README.md) for the full project overview.

## Model-provider benchmarks

The orchestrator reserves the selected worker/model pair, including lanes created
after the reservation. The internal benchmark runner drains
existing traffic, and allows only authenticated benchmark requests through those
lanes until job cleanup. A batch retains ownership between runs. Failed lane
preparation applies requested serving overrides before another startup attempt.

See [Configurable model benchmarks](docs/benchmark-configuration.md) for the API,
reservation lifecycle, configuration persistence, implementation entry points,
and Dev verification steps.

The [screenshot walkthrough](../docs/admin/model-benchmarks.md) shows
configuration, confirmation, results and GPU limits in the web application.

## Nightly benchmarks

The application runs calibration first, then opt-in benchmarks if time remains.
A shared 15-second loop drives both. `LOGOS_MAINTENANCE_MAX_NODES` limits simultaneous
maintenance workers, including manual starts. It defaults to `1`; `2` permits two
workers in parallel. Calibration and benchmark phases remain separate.
Schedules default to disabled and are stored in PostgreSQL by the webservice's
`065_nightly_benchmarks.xml` changelog. It also adds durable calibration and
manual benchmark claims, keyed by provider and job. Worker confirmation is
required before a calibration claim is released. Schema preconditions adopt existing tables
when upgrading from earlier changeSet IDs. All changeSets provide rollbacks.

The scheduler admits at most two enabled model/worker pairs per Berlin date.
A cluster lock owns each bounded parallel batch, and persisted date claims retain the quota
across restarts. Worker recovery takes priority over admission. The nightly
path reuses the existing GuideLLM job, inference logging and result storage.

| Module | Responsibility |
|---|---|
| `benchmarks/maintenance.py` | Shared admission and confirmed calibration completion |
| `benchmarks/nightly.py` | Time policy, progress watchdog and fixed parameter plan |
| `benchmarks/nightly_scheduler.py` | Pair rotation, worker preparation and restoration |
| `benchmarks/nightly_store.py` | Persistent opt-in, daily quota, cluster lock and recovery journal |
| `benchmarks/progress.py` | Measured response progress; excludes transport heartbeats |
| `benchmarks/reservations.py` | Worker/model ownership and authenticated benchmark requests |
| `routers/nightly_benchmarks.py` | Internal status and activation API |

See the [nightly benchmark guide](../docs/admin/nightly-benchmarks.md) for the complete
policy, operator workflow and test commands. See
[configurable benchmarks](docs/benchmark-configuration.md) for manual runs.

## Billing after a streaming disconnect

After a client disconnects, the orchestrator reads cloud provider streams in the
background until final usage, stream end, or the drain timeout.
`LOGOS_STREAM_DRAIN_TIMEOUT_SECONDS` bounds this wait (default: 120 seconds).
Without final usage, prompt and received output text are estimated and stored
with `usage_estimated=1` in the usage entries.

Self-hosted worker streams are cancelled immediately and their scheduler slots
released during disconnect cleanup. They are billed from usage already received,
or from the request prompt and received output text with `usage_estimated=1`.

## Worker stream buffer limits

Worker receive buffers are bounded per stream and per connected worker session.
The following environment variables configure the limits (positive integers;
byte limits refer to decoded chunks):

| Variable | Default | Limit |
|---|---:|---|
| `LOGOSNODE_STREAM_MAX_CHUNKS` | 512 | Buffered chunks per stream |
| `LOGOSNODE_STREAM_MAX_BYTES` | 8388608 (8 MiB) | Buffered bytes per stream |
| `LOGOSNODE_STREAM_MAX_CHUNK_BYTES` | 1048576 (1 MiB) | Bytes per chunk |
| `LOGOSNODE_SESSION_MAX_STREAM_BYTES` | 67108864 (64 MiB) | Buffered bytes across a session's streams |

An overflow discards queued data of active streams, fails them as upstream
errors, logs the exceeded limit and closes the worker connection. Consuming or
closing a stream releases its buffer budget.

## Node deployment maintenance

A node manager can hold one cluster-wide node deployment lease. The lease expires
30 minutes after its last renewal. `LOGOS_NODE_MAINTENANCE_MAX_LEASE_S` also limits
the elapsed time since that deployment first acquired a lease (default: 7200 seconds).
Neither status polling nor reacquiring a released or expired lease resets this deadline.
A different deployment cannot take over an active lease.

Deployment reports require a previously granted lease on the authenticated node.
`LOGOS_NODE_MAINTENANCE_REPORTS_PER_HOUR` limits report attempts per node in a rolling
hour (default: 20). Excess reports and reports without lease evidence return
`status: dropped`, log the reason, and emit no notification. Failed notification
attempts count toward this limit, but can be retried within the budget. Tags follow
the node manager's Docker tag grammar; message and requester limits are 2000 and
300 characters respectively.

Logos admins can inspect and revoke leases from Statistics → Maintenance. The
application server records these releases in the audit log. Operator API keys can
also use `POST /logosdb/providers/logosnode/node-maintenance/admin/status` with
`logos_key`, and `POST .../admin/release` with `logos_key`, `provider_id`, and
`deployment_id`. Both require a `logos_admin` key. The selected deployment cannot
reacquire a revoked lease; a stale selection cannot release a newer deployment.
Stop the node manager's deployment before releasing its lease because the node
becomes eligible for requests again.

Leases, deployment history, revocations, and report budgets are local to the
orchestrator process and reset on restart. History retains at most 4096 deployment
IDs per node until restart. Further new IDs are refused with
`deployment_history_full`, preserving existing deadlines and revocations without
unbounded memory growth.
