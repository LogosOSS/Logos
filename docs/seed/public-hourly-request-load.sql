-- For a disposable documentation database only. No inference requests are made.
BEGIN;
DELETE FROM log_entry WHERE request_id LIKE 'docs-public-hourly-%';

INSERT INTO teams (name, show_on_public_stats, public_category)
SELECT name, published, category
FROM (VALUES ('Docs Hourly A', true, 'Teaching'), ('Docs Hourly B', false, 'Research')) source(name, published, category)
WHERE NOT EXISTS (SELECT 1 FROM teams t WHERE t.name = source.name);

INSERT INTO providers (name, base_url, provider_type, auth_name, auth_format)
SELECT 'Docs Local Arrivals', 'http://docs-local.invalid', 'logosnode', 'Authorization', 'Bearer {}'
WHERE NOT EXISTS (SELECT 1 FROM providers WHERE name = 'Docs Local Arrivals');

WITH samples AS (
    SELECT bucket,
           EXTRACT(HOUR FROM bucket AT TIME ZONE 'Europe/Berlin')::int AS local_hour,
           g, t.id AS team_id, p.id AS provider_id
    FROM generate_series(date_trunc('hour', now(), 'UTC') - interval '8 days',
                         date_trunc('hour', now(), 'UTC') - interval '25 hours', interval '1 hour') AS v(bucket)
    CROSS JOIN generate_series(1, 93) g
    CROSS JOIN teams t
    CROSS JOIN providers p
    WHERE t.name IN ('Docs Hourly A', 'Docs Hourly B') AND p.name = 'Docs Local Arrivals'
)
INSERT INTO log_entry (request_id, team_id, provider_id, api_key_id, timestamp_request, timestamp_response,
                       result_status, benchmark_job_id, environment)
SELECT 'docs-public-hourly-' || team_id || '-' || EXTRACT(EPOCH FROM bucket)::bigint || '-' || g,
    team_id, provider_id,
    CASE WHEN g % 11 = 0 THEN (SELECT id FROM api_keys WHERE internal_service = 'agent') END,
    bucket + make_interval(mins => g % 60),
    bucket + make_interval(mins => g % 60, secs => 1),
    (CASE WHEN g % 13 = 0 THEN 'error' ELSE 'success' END)::result_status_enum,
    CASE WHEN g % 17 = 0 THEN 998 END, 'docs-role-screenshots'
FROM samples
WHERE g <= CASE WHEN local_hour BETWEEN 8 AND 19 THEN 92 - 7 * abs(13 - local_hour)
                ELSE (local_hour * 3) % 13 END;
COMMIT;
