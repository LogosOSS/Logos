-- Optional synthetic Agent Sessions screenshot fixture. Only load in the disposable docs stack.
-- The fake origin is intentionally not usable for inference.
BEGIN;

INSERT INTO providers (name, base_url, provider_type, cloud_provider_type, privacy_level, auth_name, auth_format, api_key)
SELECT 'Docs Logos Gateway', 'https://inference-example.invalid', 'cloud', 'logos',
       'CLOUD_IN_EU_BY_US_PROVIDER', 'Authorization', 'Bearer {}', 'docs-fake-not-a-key'
WHERE NOT EXISTS (SELECT FROM providers WHERE base_url = 'https://inference-example.invalid');

INSERT INTO models (name, description)
SELECT 'agent-sample', 'Example model behind the documentation gateway.'
WHERE NOT EXISTS (SELECT FROM models WHERE name = 'agent-sample');

INSERT INTO model_provider (model_id, provider_id)
SELECT m.id, p.id FROM models m CROSS JOIN providers p
WHERE m.name = 'agent-sample' AND p.base_url = 'https://inference-example.invalid'
ON CONFLICT DO NOTHING;
COMMIT;
