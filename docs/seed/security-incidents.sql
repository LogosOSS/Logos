-- Illustrative incident states for the isolated documentation stack.
-- These rows do not describe the security posture of a running deployment.
BEGIN;
DELETE FROM security_incidents WHERE created_by = 'docs-role-screenshots';
DELETE FROM security_repositories WHERE created_by = 'docs-role-screenshots';
INSERT INTO security_repositories (repo_slug, branch, created_by) VALUES
  ('docs-example/logos', 'main', 'docs-role-screenshots'),
  ('docs-example/public-portal', 'main', 'docs-role-screenshots');
INSERT INTO security_incidents
  (advisory_id, title, severity, known_exploited, source_url, summary, importance, created_by)
VALUES
  ('DEMO-SECURITY-NGINX', 'HTTP/2 memory corruption in bundled nginx servers', 'critical', false,
   'https://nginx.org/en/security_advisories.html',
   'Illustrative assessment of nginx in application images, the edge proxy and internal gateways.',
   'Remote code execution risk requires a check of every proxy hop and bundled server.', 'docs-role-screenshots'),
  ('DEMO-SECURITY-PHP', 'Critical PHP request parsing vulnerability', 'critical', false,
   'https://www.php.net/',
   'Illustrative temporary containment decision for an exposed PHP service.',
   'The public portal requires an immediate containment decision while a compatible patch is unavailable.', 'docs-role-screenshots');
INSERT INTO security_assessments (incident_id, repository_id, status, revision, report, next_check_at)
SELECT i.id, r.id,
  CASE
    WHEN r.repo_slug = 'docs-example/logos' AND i.advisory_id <> 'DEMO-SECURITY-JAVA' THEN 'not_affected'
    WHEN i.advisory_id = 'DEMO-SECURITY-PHP' THEN 'action_required'
    ELSE 'awaiting_upstream'
  END, i.revision,
  jsonb_build_object(
    'commit_sha', repeat('a', 40),
    'rationale', 'Illustrative documentation result. Check the actual image digest and runtime configuration before a production decision.',
    'evidence', jsonb_build_array(jsonb_build_object('path', 'docker-compose.yaml',
      'finding', 'Inspect every image and internal proxy hop. Repository configuration does not prove which image is currently deployed.')),
    'action', CASE WHEN i.advisory_id='DEMO-SECURITY-PHP' AND r.repo_slug='docs-example/public-portal' THEN 'disable_component' ELSE 'none' END,
    'recommended_action', CASE
      WHEN r.repo_slug='docs-example/logos' AND i.advisory_id='DEMO-SECURITY-NGINX'
        THEN 'The checked nginx listeners do not enable HTTP/2. Verify nginx -T and the image digest on the running gateways before relying on this result.'
      WHEN r.repo_slug='docs-example/logos' AND i.advisory_id='DEMO-SECURITY-PHP'
        THEN 'No PHP runtime appears in the checked application images. Reassess when the repository or deployment inventory changes.'
      WHEN i.advisory_id='DEMO-SECURITY-PHP'
        THEN 'Temporarily disable the public PHP portal. Keep the internal API available. Restore access after a patched image passes validation; record the deployment evidence.'
      ELSE 'Wait for a corrected vendor image. Keep the documented mitigation active, then test and propose an exact image update when the release is available.'
    END),
  now() + interval '6 hours'
FROM security_incidents i CROSS JOIN security_repositories r
WHERE i.created_by='docs-role-screenshots' AND r.created_by='docs-role-screenshots'
  AND (i.advisory_id='DEMO-SECURITY-NGINX' OR r.repo_slug='docs-example/public-portal');
INSERT INTO security_assessment_events (assessment_id, status, detail, created_by)
SELECT id, status, jsonb_build_object('reason', 'Illustrative assessment for the role guide.'), 'docs-role-screenshots'
FROM security_assessments WHERE incident_id IN (SELECT id FROM security_incidents WHERE created_by='docs-role-screenshots');
COMMIT;
