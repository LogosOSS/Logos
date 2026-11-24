-- Illustrative deployments for the isolated documentation stack.
-- These rows do not describe a real deployment of Logos.
--
-- Idempotent. Apply it after role-screenshots.sql, because the worker deployer belongs to the provider
-- "Docs Local Worker" of that seed:
--   docker exec -i doks-db psql -U postgres -d logosdb < docs/seed/deployments.sql
--
-- The seed makes two deployers (a core host and the documentation worker) and four finished deployments:
--   #139 1.0.0   succeeded
--   #140 1.0.1   rolled back (the new worker container did not connect)
--   #141 1.0.12  failed (the image of the tag does not exist)
--   #142 1.0.2   succeeded
-- The deployment that runs (#143) is not here. capture-screenshots.cjs adds it with deployments-running.sql for one
-- shot and removes it again, because a running deployment switches the button Deploy off.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM providers WHERE name = 'Docs Local Worker') THEN
        RAISE EXCEPTION 'The provider "Docs Local Worker" is missing: apply role-screenshots.sql first';
    END IF;
END $$;

-- Wipe the previous run. The steps go with their plans, and the worker deployer goes with its provider.
DELETE FROM deployment_plans WHERE number BETWEEN 139 AND 143;
DELETE FROM deployers
WHERE name IN ('Docs core host', 'Docs standby host')
   OR provider_id = (SELECT id FROM providers WHERE name = 'Docs Local Worker');

-- A core deployer has a key of its own, and Logos stores only its hash. The key of this one is made up.
INSERT INTO deployers (id, name, kind, key_hash, created_at, last_seen_at, version)
VALUES (gen_random_uuid(), 'Docs core host', 'core',
        encode(sha256('docs-role-screenshots-core-key'::bytea), 'hex'),
        now() - interval '30 days', now(), '0.1.0');

-- A worker deployer takes the name of its provider. It exists as soon as its node manager asked for orders.
INSERT INTO deployers (id, name, kind, provider_id, created_at, last_seen_at, version)
SELECT gen_random_uuid(), p.name, 'worker', p.id, now() - interval '30 days', now(), '0.1.0'
FROM providers p
WHERE p.name = 'Docs Local Worker';

-- One event of a node: the time is `seconds` after the start of the deployment.
CREATE FUNCTION pg_temp.ev(base timestamptz, seconds integer, phase text, note text) RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_build_object(
        'at', to_char((base + make_interval(secs => seconds)) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
        'phase', phase,
        'note', note)
$$;

-- The events of a core node that succeeded.
CREATE FUNCTION pg_temp.core_events(base timestamptz, tag text) RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_build_array(
        pg_temp.ev(base, 3, 'queued', 'using the compose file core.yaml of tag ' || tag || ' (sha256 47b5ca620cb1)'),
        pg_temp.ev(base, 3, 'pulling', '8 of 8 container(s) run now; deploying tag ' || tag),
        pg_temp.ev(base, 5, 'draining', 'pulled the images for tag ' || tag),
        pg_temp.ev(base, 19, 'verifying', 'started the services for tag ' || tag),
        pg_temp.ev(base, 21, 'verifying', 'waiting: logos-rate-gateway runs 0 of 1 ready copy'),
        pg_temp.ev(base, 98, 'done', 'succeeded: now running tag ' || tag))
$$;

-- The first events of a worker node, up to the swap. `before` is the tag that ran until now.
CREATE FUNCTION pg_temp.worker_start_events(base timestamptz, seconds integer, tag text, before text, drained integer)
RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_build_array(
        pg_temp.ev(base, seconds, 'queued', 'using the compose file worker.yml of tag ' || tag || ' (sha256 b57f17c57b06)'),
        pg_temp.ev(base, seconds, 'pulling',
                   'running ghcr.io/logososs/logos-workernode-vllm:' || before || ' (06c92f16c943); deploying tag ' || tag),
        pg_temp.ev(base, seconds + 14, 'draining',
                   'pulled ghcr.io/logososs/logos-workernode-vllm:' || tag || ' (6cde94b6f62c)'),
        pg_temp.ev(base, seconds + 14, 'draining',
                   'cordoned the node at the orchestrator; no other node serves llama-3.1-8b-instruct, '
                   || 'so requests for it wait until this node is back'),
        pg_temp.ev(base, seconds + 14, 'draining',
                   'waiting for the node to drain: 2 request(s) (2 tracked by the orchestrator, 1 on the worker) running'),
        pg_temp.ev(base, seconds + 14 + drained, 'draining', 'the node has drained after ' || drained || ' s'),
        pg_temp.ev(base, seconds + 14 + drained, 'swapping', 'the node has drained; replacing the container'),
        pg_temp.ev(base, seconds + 19 + drained, 'verifying', 'started container 451de673bec6'))
$$;

DO $$
DECLARE
    core_id uuid := (SELECT id FROM deployers WHERE name = 'Docs core host');
    worker_id uuid := (SELECT d.id FROM deployers d JOIN providers p ON p.id = d.provider_id
                       WHERE p.name = 'Docs Local Worker');
    plan uuid;
    base timestamptz;
    sole jsonb := '["llama-3.1-8b-instruct"]'::jsonb;
BEGIN
    -- #139: 1.0.0 succeeded.
    plan := gen_random_uuid();
    base := now() - interval '12 days';
    INSERT INTO deployment_plans (id, number, tag, status, initiator, created_at, finished_at, last_activity_at)
    VALUES (plan, 139, '1.0.0', 'succeeded', 'Tobias Wasner', base, base + interval '214 seconds',
            base + interval '214 seconds');
    INSERT INTO deployment_steps (id, plan_id, position, kind, deployer_id, deployer_name, status, phase,
                                  started_at, finished_at, last_activity_at, events, details)
    VALUES
        (gen_random_uuid(), plan, 0, 'core', core_id, 'Docs core host', 'succeeded', 'done',
         base + interval '3 seconds', base + interval '98 seconds', base + interval '98 seconds',
         pg_temp.core_events(base, '1.0.0'), '{}'::jsonb),
        (gen_random_uuid(), plan, 1, 'worker', worker_id, 'Docs Local Worker', 'succeeded', 'done',
         base + interval '102 seconds', base + interval '214 seconds', base + interval '214 seconds',
         pg_temp.worker_start_events(base, 102, '1.0.0', '0.9.9', 9)
         || jsonb_build_array(
                pg_temp.ev(base, 195, 'verifying', 'the worker is connected to the orchestrator'),
                pg_temp.ev(base, 195, 'done',
                           'succeeded: now running ghcr.io/logososs/logos-workernode-vllm:1.0.0')),
         jsonb_build_object('drained_requests', 2, 'drain_seconds', 9, 'sole_replica_models', sole));

    -- #140: 1.0.1 rolled back. The new worker container never connected, so the node manager started the old one.
    plan := gen_random_uuid();
    base := now() - interval '7 days';
    INSERT INTO deployment_plans (id, number, tag, status, initiator, reason, created_at, finished_at,
                                  last_activity_at)
    VALUES (plan, 140, '1.0.1', 'rolled_back', 'Tobias Wasner',
            'Docs Local Worker: the new container did not connect to the orchestrator within 600 s. '
            || 'The previous version runs again.',
            base, base + interval '790 seconds', base + interval '790 seconds');
    INSERT INTO deployment_steps (id, plan_id, position, kind, deployer_id, deployer_name, status, phase, failure,
                                  message, started_at, finished_at, last_activity_at, events, failure_logs, details)
    VALUES
        (gen_random_uuid(), plan, 0, 'core', core_id, 'Docs core host', 'succeeded', 'done', NULL, NULL,
         base + interval '3 seconds', base + interval '98 seconds', base + interval '98 seconds',
         pg_temp.core_events(base, '1.0.1'), NULL, '{}'::jsonb),
        (gen_random_uuid(), plan, 1, 'worker', worker_id, 'Docs Local Worker', 'rolled_back', 'done', 'verify_failed',
         'the new container did not connect to the orchestrator within 600 s. The previous version runs again.',
         base + interval '102 seconds', base + interval '790 seconds', base + interval '790 seconds',
         pg_temp.worker_start_events(base, 102, '1.0.1', '1.0.0', 7)
         || jsonb_build_array(
                pg_temp.ev(base, 726, 'rolling_back',
                           'the new container did not connect to the orchestrator within 600 s; '
                           || 'starting the previous version'),
                pg_temp.ev(base, 790, 'done',
                           'rolled back: now running ghcr.io/logososs/logos-workernode-vllm:1.0.0')),
         -- The last lines of the new container, in UTC, from its start (the swap ended at 128 s) to its stop.
         (SELECT string_agg(
                     to_char((base + make_interval(secs => l.s)) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
                     || ',' || l.ms || ' ' || l.line,
                     E'\n' ORDER BY l.s, l.ms)
          FROM (VALUES
              (287, '102', 'WARNING logos_worker_node.bridge: the server refused the key (HTTP 401); retrying in 120 s'),
              (407, '338', 'WARNING logos_worker_node.bridge: the server refused the key (HTTP 401); retrying in 120 s'),
              (527, '571', 'WARNING logos_worker_node.bridge: the server refused the key (HTTP 401); retrying in 120 s'),
              (647, '804', 'WARNING logos_worker_node.bridge: the server refused the key (HTTP 401); retrying in 120 s'),
              (726, '377', 'ERROR logos_worker_node.bridge: no connection to the orchestrator'),
              (727, '012', 'INFO logos_worker_node: received SIGTERM, shutting down')
          ) AS l(s, ms, line)),
         jsonb_build_object('drained_requests', 2, 'drain_seconds', 7, 'sole_replica_models', sole));

    -- #141: 1.0.12 failed. The tag does not exist, so the pull of the core failed and the worker did not start.
    plan := gen_random_uuid();
    base := now() - interval '3 days';
    INSERT INTO deployment_plans (id, number, tag, status, initiator, reason, created_at, finished_at,
                                  last_activity_at)
    VALUES (plan, 141, '1.0.12', 'failed', 'Tobias Wasner',
            'Docs core host: docker compose pull failed: failed to resolve reference '
            || '"ghcr.io/logososs/logos-webservice:1.0.12": not found. The running services were not touched.',
            base, base + interval '8 seconds', base + interval '8 seconds');
    INSERT INTO deployment_steps (id, plan_id, position, kind, deployer_id, deployer_name, status, phase, failure,
                                  message, started_at, finished_at, last_activity_at, events, details)
    VALUES
        (gen_random_uuid(), plan, 0, 'core', core_id, 'Docs core host', 'failed', 'done', 'pull_failed',
         'docker compose pull failed: failed to resolve reference "ghcr.io/logososs/logos-webservice:1.0.12": '
         || 'not found. The running services were not touched.',
         base + interval '3 seconds', base + interval '8 seconds', base + interval '8 seconds',
         jsonb_build_array(
             pg_temp.ev(base, 3, 'queued', 'the image of the tag carries no compose file: using the compose file of the host'),
             pg_temp.ev(base, 3, 'pulling', '8 of 8 container(s) run now; deploying tag 1.0.12'),
             pg_temp.ev(base, 8, 'done',
                        'failed: docker compose pull failed: failed to resolve reference '
                        || '"ghcr.io/logososs/logos-webservice:1.0.12": not found. The running services were not touched.')),
         '{}'::jsonb),
        (gen_random_uuid(), plan, 1, 'worker', worker_id, 'Docs Local Worker', 'cancelled', NULL, NULL,
         'Not started: the core did not succeed.',
         NULL, base + interval '8 seconds', base + interval '8 seconds', '[]'::jsonb, '{}'::jsonb);

    -- #142: 1.0.2 succeeded: the last deployment, and the one that the page shows when nothing runs.
    plan := gen_random_uuid();
    base := now() - interval '5 hours';
    INSERT INTO deployment_plans (id, number, tag, status, initiator, created_at, finished_at, last_activity_at)
    VALUES (plan, 142, '1.0.2', 'succeeded', 'Tobias Wasner', base, base + interval '221 seconds',
            base + interval '221 seconds');
    INSERT INTO deployment_steps (id, plan_id, position, kind, deployer_id, deployer_name, status, phase,
                                  started_at, finished_at, last_activity_at, events, details)
    VALUES
        (gen_random_uuid(), plan, 0, 'core', core_id, 'Docs core host', 'succeeded', 'done',
         base + interval '3 seconds', base + interval '98 seconds', base + interval '98 seconds',
         pg_temp.core_events(base, '1.0.2'), '{}'::jsonb),
        (gen_random_uuid(), plan, 1, 'worker', worker_id, 'Docs Local Worker', 'succeeded', 'done',
         base + interval '102 seconds', base + interval '221 seconds', base + interval '221 seconds',
         pg_temp.worker_start_events(base, 102, '1.0.2', '1.0.0', 12)
         || jsonb_build_array(
                pg_temp.ev(base, 221, 'verifying', 'the worker is connected to the orchestrator'),
                pg_temp.ev(base, 221, 'done',
                           'succeeded: now running ghcr.io/logososs/logos-workernode-vllm:1.0.2')),
         jsonb_build_object('drained_requests', 2, 'drain_seconds', 12, 'sole_replica_models', sole));
END $$;

-- The next deployment gets the number after the last one.
SELECT setval('deployment_plan_number_seq', 142);

COMMIT;
