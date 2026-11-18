"""Security outcomes fail closed and remediation never implies deployment."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from app import auth, main
from app import security_incidents as incidents
from app import security_store
from app.security_models import AssessmentReport, IncidentInput, RepositoryInput
from app.security_scans import ScanInput, important_findings
from pydantic import ValidationError

SHA = "a" * 40


def report(**overrides):
    return {
        "outcome": "not_affected",
        "commit_sha": SHA,
        "rationale": (
            "The repository contains no PHP runtime or image, " "as confirmed by the complete deployment configuration."
        ),
        "evidence": [
            {
                "path": "docker-compose.yaml",
                "finding": "All service images use Java, Python or nginx; none contains a PHP runtime.",
            }
        ],
        "recommended_action": "No source change is required for this repository.",
        "action": "none",
        **overrides,
    }


@pytest.mark.parametrize("outcome", ["resolved", "mitigated", "fixed", "unknown"])
def test_agent_cannot_claim_operator_outcomes(outcome):
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate(report(outcome=outcome))


def test_not_affected_requires_positive_repository_evidence():
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate(report(evidence=[]))
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate(report(affected_components=["php"]))


def test_patch_must_name_released_version_and_source():
    candidate = report(outcome="fix_available", action="upgrade", affected_components=["nginx"])
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate(candidate)
    assert AssessmentReport.model_validate(
        {**candidate, "patched_version": "1.31.9", "patch_source": "https://nginx.org/en/security_advisories.html"}
    )


def test_shutdown_is_an_operator_recommendation():
    candidate = report(outcome="awaiting_upstream", action="shutdown_system", affected_components=["portal"])
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate(candidate)
    assert AssessmentReport.model_validate({**candidate, "outcome": "action_required"})


@pytest.mark.parametrize(
    "slug", ["https://github.com/org/repo", "org/../repo", "org/repo.git", "org/..", "org/repo?token=x"]
)
def test_repository_scope_is_a_canonical_github_slug(slug):
    with pytest.raises(ValidationError):
        RepositoryInput(repo_slug=slug)


def test_low_severity_incidents_are_not_admitted():
    with pytest.raises(ValidationError):
        IncidentInput(
            advisory_id="CVE-2026-12345",
            title="Minor vulnerability",
            severity="medium",
            source_url="https://example.org/advisory",
            summary="A small denial of service issue.",
            importance="This is a routine dependency update.",
        )


def test_unfixed_critical_scan_is_retained_and_noise_excluded():
    body = ScanInput.model_validate(
        {
            "repository_id": 1,
            "image_digest": "sha256:" + "a" * 64,
            "scanned_at": datetime.now(timezone.utc),
            "report": {
                "Results": [
                    {
                        "Target": "service-image",
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": "CVE-2026-12345",
                                "PkgName": "nginx",
                                "InstalledVersion": "1.0",
                                "Severity": "CRITICAL",
                            },
                            {
                                "VulnerabilityID": "CVE-2026-12346",
                                "PkgName": "libfoo",
                                "InstalledVersion": "1.0",
                                "Severity": "MEDIUM",
                            },
                        ],
                    }
                ]
            },
        }
    )
    findings = important_findings(body)
    assert list(findings) == ["CVE-2026-12345"]
    assert findings["CVE-2026-12345"][0]["fixed_version"] is None


def test_report_is_bound_to_trusted_checkout(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts" / "7"
    state = tmp_path / "state" / "7"
    artifacts.mkdir(parents=True)
    state.mkdir(parents=True)
    (artifacts / "security-assessment.json").write_text(json.dumps(report()))
    (state / "checkout_baseline.json").write_text(json.dumps({"checkout_sha": SHA}))
    monkeypatch.setattr(
        incidents,
        "settings",
        replace(incidents.settings, artifact_root=str(artifacts.parent), state_root=str(state.parent)),
    )
    assert incidents.read_report(7).outcome == "not_affected"
    (state / "checkout_baseline.json").write_text(json.dumps({"checkout_sha": "b" * 40}))
    with pytest.raises(ValueError, match="trusted checkout"):
        incidents.read_report(7)


@pytest.mark.parametrize("role", ["app_admin", "app_developer"])
async def test_every_security_endpoint_rejects_non_logos_admins(role):
    main.app.dependency_overrides[auth.current_principal] = lambda: auth.Principal("u", "u", frozenset({role}))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            for method, path in [
                ("GET", "/security"),
                ("GET", "/security/assessments/1/events"),
                ("POST", "/security/repositories"),
                ("POST", "/security/incidents"),
                ("POST", "/security/assessments/1/actions"),
                ("POST", "/security/scans/trivy"),
            ]:
                response = await client.request(method, path, json={} if method == "POST" else None)
                assert response.status_code == 403, (path, response.text)
    finally:
        main.app.dependency_overrides.clear()


class Result:
    def scalar_one_or_none(self):
        return 1


class Connection:
    def __init__(self):
        self.writes = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, query, params):
        self.writes.append((str(query), params))
        return Result()

    async def commit(self):
        pass


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"merged": True, "state": "closed"}, "awaiting_deployment"),
        ({"merged": False, "state": "closed"}, "action_required"),
    ],
)
async def test_pr_state_does_not_claim_deployed_fix(monkeypatch, state, expected):
    conn = Connection()
    monkeypatch.setattr(incidents.db, "sessionmaker", lambda: lambda: conn)
    monkeypatch.setattr(incidents.github, "_get", AsyncMock(return_value=state))
    monkeypatch.setattr(security_store, "event", AsyncMock())
    await incidents.reconcile_one(
        {
            "id": 1,
            "status": "pr_open",
            "session_status": "succeeded",
            "report": {},
            "pr_url": "https://github.com/example/repo/pull/3",
            "repo_slug": "example/repo",
            "session_id": 4,
        }
    )
    assert conn.writes[0][1]["status"] == expected
    incidents.github._get.assert_awaited_once_with("/repos/example/repo/pulls/3")


async def test_failed_verification_preserves_draft_without_claiming_success(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(incidents.db, "sessionmaker", lambda: lambda: conn)
    monkeypatch.setattr(security_store, "event", AsyncMock())
    await incidents.reconcile_one(
        {
            "id": 1,
            "status": "remediating",
            "session_status": "failed",
            "report": {},
            "pr_url": None,
            "session_pr_url": "https://github.com/example/repo/pull/4",
            "session_id": 2,
            "session_error": "Compiler verification failed",
        }
    )
    assert conn.writes[0][1]["status"] == "failed"
    assert conn.writes[0][1]["pr"].endswith("/pull/4")


@pytest.mark.parametrize("kind", ["security-analysis", "security-fix"])
async def test_general_session_retries_cannot_detach_security_work(monkeypatch, kind):
    from app import sessions

    create = AsyncMock()
    watch = AsyncMock()
    monkeypatch.setattr(sessions.db, "create_session", create)
    monkeypatch.setattr(sessions.db, "update_session", watch)
    session = {"trigger_kind": kind, "trigger_ref": "security:1:2", "branch_name": "fix"}
    assert not await sessions.manager._may_try_again(session)
    assert await sessions.manager.take_up_again(session) is None
    await sessions.manager._watch_the_checks(session, SHA)
    create.assert_not_awaited()
    watch.assert_not_awaited()
