-- For the disposable screenshots database only. No inference requests are made.
BEGIN;
DELETE FROM log_entry WHERE request_id LIKE 'docs-public-hourly-%';

INSERT INTO teams (name, show_on_public_stats, public_category)
SELECT name, true, category
FROM (VALUES ('Docs Hourly A', 'Teaching'), ('Docs Hourly B', 'Research'), ('Docs Hourly C', 'Teaching')) source(name, category)
WHERE NOT EXISTS (SELECT 1 FROM teams t WHERE t.name = source.name);

WITH monday AS (
    SELECT DATE '1970-01-05' + 28 * FLOOR(
        (date_trunc('week', now() AT TIME ZONE 'Europe/Berlin')::date - 7 - DATE '1970-01-05') / 28.0
    )::int AS block_end
), profile(hour, requests) AS (
    VALUES (0, 14), (1, 12), (3, 6), (4, 14), (5, 22), (6, 56), (7, 112), (8, 168),
        (9, 300), (10, 470), (11, 510), (12, 420), (13, 390), (14, 490), (15, 525),
        (16, 370), (17, 315), (18, 225), (19, 150), (20, 145), (21, 105), (22, 77), (23, 39)
)
INSERT INTO log_entry (request_id, team_id, timestamp_request, timestamp_response, result_status, environment)
SELECT 'docs-public-hourly-' || t.id || '-' || p.hour || '-' || g.n, t.id, sample.at,
    sample.at + interval '1 second', 'success', 'docs-role-screenshots'
FROM teams t CROSS JOIN profile p CROSS JOIN monday CROSS JOIN LATERAL generate_series(1, p.requests) g(n)
CROSS JOIN LATERAL (
    SELECT (monday.block_end - 1 - (g.n % 28) + make_time(p.hour, 20, 0)) AT TIME ZONE 'Europe/Berlin' AS at
) sample
WHERE t.name IN ('Docs Hourly A', 'Docs Hourly B', 'Docs Hourly C');
COMMIT;
