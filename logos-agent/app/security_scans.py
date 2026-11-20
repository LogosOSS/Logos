"""Import critical Trivy findings as evidence, including unfixed packages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from . import db, security_store
from .security_models import IncidentInput


class TrivyVulnerability(BaseModel):
    vulnerability_id: str = Field(
        alias="VulnerabilityID", max_length=100, pattern=r"^(CVE-[0-9]{4}-[0-9]+|GHSA-[A-Za-z0-9-]+)$"
    )
    package: str = Field(alias="PkgName", min_length=1, max_length=300)
    installed: str = Field(alias="InstalledVersion", max_length=300)
    fixed: str = Field(alias="FixedVersion", default="", max_length=300)
    severity: str = Field(alias="Severity", max_length=30)
    title: str = Field(alias="Title", default="", max_length=1000)
    description: str = Field(alias="Description", default="", max_length=16000)


class TrivyResult(BaseModel):
    target: str = Field(alias="Target", max_length=1000)
    vulnerabilities: list[TrivyVulnerability] = Field(alias="Vulnerabilities", default_factory=list, max_length=5000)


class TrivyReport(BaseModel):
    results: list[TrivyResult] = Field(alias="Results", default_factory=list, max_length=100)


class ScanInput(BaseModel):
    repository_id: int = Field(gt=0)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    scanned_at: datetime
    report: TrivyReport

    @field_validator("scanned_at")
    @classmethod
    def timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Provide the actual scan time with a timezone")
        return value


def important_findings(body: ScanInput) -> dict[str, list[dict]]:
    findings: dict[str, list[dict]] = {}
    for result in body.report.results:
        for item in result.vulnerabilities:
            # Ordinary high findings are noisy. Known exploited highs arrive
            # through KEV, or an operator supplies material impact at intake.
            if item.severity.upper() != "CRITICAL":
                continue
            findings.setdefault(item.vulnerability_id.upper(), []).append(
                {
                    "target": result.target,
                    "package": item.package,
                    "installed_version": item.installed,
                    "fixed_version": item.fixed or None,
                    "title": item.title,
                    "description": item.description,
                }
            )
    if len(findings) > 100:
        raise ValueError("Import at most 100 critical vulnerabilities per request")
    return findings


async def ingest_scan(body: ScanInput, by: str) -> dict:
    findings = important_findings(body)
    async with db.sessionmaker()() as conn:
        exists = (
            await conn.execute(text("SELECT id FROM security_repositories WHERE id=:id"), {"id": body.repository_id})
        ).scalar_one_or_none()
    if exists is None:
        raise ValueError("Register the repository before importing its scan")
    accepted = []
    for advisory_id, evidence in findings.items():
        first = evidence[0]
        source = (
            f"https://nvd.nist.gov/vuln/detail/{advisory_id}"
            if advisory_id.startswith("CVE-")
            else f"https://github.com/advisories/{advisory_id}"
        )
        incident = await security_store.ingest(
            IncidentInput(
                advisory_id=advisory_id,
                title=(first["title"] or f"Critical vulnerability {advisory_id}")[:300],
                severity="critical",
                source_url=source,
                summary=(
                    first["description"] or f"Trivy reported critical vulnerability {advisory_id}; validate exposure."
                )[:16000],
                importance=(
                    "Critical image scan finding. Verify package reachability "
                    "and runtime exposure before remediation."
                ),
            ),
            "scanner:trivy",
        )
        async with db.sessionmaker()() as conn:
            changed = (
                await conn.execute(
                    text("""
                INSERT INTO security_scan_observations
                    (incident_id, repository_id, image_digest, scanned_at, findings, created_by)
                VALUES (:incident, :repo, :digest, :scanned, CAST(:findings AS jsonb), :by)
                ON CONFLICT (incident_id, repository_id, image_digest) DO UPDATE SET
                    scanned_at=EXCLUDED.scanned_at, findings=EXCLUDED.findings, created_by=EXCLUDED.created_by
                WHERE security_scan_observations.scanned_at < EXCLUDED.scanned_at
                RETURNING id
            """),
                    {
                        "incident": incident["id"],
                        "repo": body.repository_id,
                        "digest": body.image_digest,
                        "scanned": body.scanned_at,
                        "findings": json.dumps(evidence),
                        "by": by,
                    },
                )
            ).scalar_one_or_none()
            if changed is not None:
                # Fresh image evidence invalidates a terminal source-only result.
                # In-flight sessions keep ownership; the revision forces a new
                # assessment after they finish without duplicate fix sessions.
                await conn.execute(
                    text("UPDATE security_incidents SET revision=revision+1, updated_at=now() WHERE id=:id"),
                    {"id": incident["id"]},
                )
            await conn.commit()
        accepted.append(incident["id"])
    return {"incident_ids": accepted, "accepted": len(accepted)}
