-- A deployment that runs now (#143), for the shot of the part "Current deployment".
-- It belongs to deployments.sql. capture-screenshots.cjs applies this file before that one shot and removes the
-- deployment again, because a running deployment switches the button Deploy off for every other shot.
--   docker exec -i doks-db psql -U postgres -d logosdb < docs/seed/deployments-running.sql
-- Remove it by hand: DELETE FROM deployment_plans WHERE number = 143;
BEGIN;

DELETE FROM deployment_plans WHERE number = 143;

-- One event of a node: the time is `seconds` after the start of the deployment.
CREATE FUNCTION pg_temp.ev(base timestamptz, seconds integer, phase text, note text) RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_build_object(
        'at', to_char((base + make_interval(secs => seconds)) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
        'phase', phase,
        'note', note)
$$;

DO $$
DECLARE
    core_id uuid := (SELECT id FROM deployers WHERE name = 'Docs core host');
    worker_id uuid := (SELECT d.id FROM deployers d JOIN providers p ON p.id = d.provider_id
                       WHERE p.name = 'Docs Local Worker');
    plan uuid := gen_random_uuid();
    base timestamptz := now() - interval '2 minutes';
BEGIN
    IF core_id IS NULL OR worker_id IS NULL THEN
        RAISE EXCEPTION 'The deployers are missing: apply deployments.sql first';
    END IF;

    INSERT INTO deployment_plans (id, number, tag, status, initiator, created_at, last_activity_at)
    VALUES (plan, 143, '1.0.3', 'running', 'Tobias Wasner', base, now());

    -- The core ended well. The worker waits for the requests of its node to end.
    INSERT INTO deployment_steps (id, plan_id, position, kind, deployer_id, deployer_name, status, phase,
                                  started_at, finished_at, last_activity_at, events, details)
    VALUES
        (gen_random_uuid(), plan, 0, 'core', core_id, 'Docs core host', 'succeeded', 'done',
         base + interval '3 seconds', base + interval '98 seconds', base + interval '98 seconds',
         jsonb_build_array(
             pg_temp.ev(base, 3, 'queued', 'using the compose file core.yaml of tag 1.0.3 (sha256 47b5ca620cb1)'),
             pg_temp.ev(base, 3, 'pulling', '8 of 8 container(s) run now; deploying tag 1.0.3'),
             pg_temp.ev(base, 5, 'draining', 'pulled the images for tag 1.0.3'),
             pg_temp.ev(base, 19, 'verifying', 'started the services for tag 1.0.3'),
             pg_temp.ev(base, 21, 'verifying', 'waiting: logos-rate-gateway runs 0 of 1 ready copy'),
             pg_temp.ev(base, 98, 'done', 'succeeded: now running tag 1.0.3')),
         '{}'::jsonb),
        (gen_random_uuid(), plan, 1, 'worker', worker_id, 'Docs Local Worker', 'running', 'draining',
         base + interval '102 seconds', NULL, now(),
         jsonb_build_array(
             pg_temp.ev(base, 102, 'queued', 'using the compose file worker.yml of tag 1.0.3 (sha256 b57f17c57b06)'),
             pg_temp.ev(base, 102, 'pulling',
                        'running ghcr.io/logososs/logos-workernode-vllm:1.0.2 (06c92f16c943); deploying tag 1.0.3'),
             pg_temp.ev(base, 116, 'draining',
                        'pulled ghcr.io/logososs/logos-workernode-vllm:1.0.3 (6cde94b6f62c)'),
             pg_temp.ev(base, 116, 'draining',
                        'cordoned the node at the orchestrator; no other node serves llama-3.1-8b-instruct, '
                        || 'so requests for it wait until this node is back'),
             pg_temp.ev(base, 116, 'draining',
                        'waiting for the node to drain: 3 request(s) (3 tracked by the orchestrator, 2 on the worker) running')),
         jsonb_build_object('drained_requests', 3, 'sole_replica_models', '["llama-3.1-8b-instruct"]'::jsonb));
END $$;

COMMIT;
