"""Validated intake and evidence contracts for security incident work."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RepositoryReference(BaseModel):
    repo_slug: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
    branch: str = Field(default="main", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9/_.-]*$")

    @field_validator("repo_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        if value.split("/")[1] in {".", ".."} or value.endswith(".git"):
            raise ValueError("Use owner/repository without .git")
        return value.lower()

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        if ".." in value or "//" in value or value.endswith(("/", ".", ".lock")):
            raise ValueError("Invalid branch")
        return value


class RepositoryInput(RepositoryReference):
    verification_command: str = Field(min_length=1, max_length=4000)

    @field_validator("verification_command")
    @classmethod
    def validate_verification_command(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A build and test command is required")
        return value


def public_https(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Source must be an HTTPS URL without credentials")
    return value


class IncidentInput(BaseModel):
    advisory_id: str = Field(min_length=5, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=8, max_length=300)
    severity: Literal["high", "critical"]
    known_exploited: bool = False
    source_url: str = Field(max_length=2000)
    summary: str = Field(min_length=20, max_length=16000)
    importance: str = Field(min_length=20, max_length=2000)
    upstream_repositories: list[str] = Field(default_factory=list, max_length=10)

    _source = field_validator("source_url")(public_https)

    @field_validator("advisory_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return value.upper()

    @field_validator("upstream_repositories")
    @classmethod
    def validate_repositories(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(RepositoryReference(repo_slug=v).repo_slug for v in values))


class Evidence(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    finding: str = Field(min_length=20, max_length=4000)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("Evidence must name a repository-relative path")
        return value


class AssessmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["not_affected", "awaiting_upstream", "fix_available", "action_required"]
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    rationale: str = Field(min_length=40, max_length=8000)
    evidence: list[Evidence] = Field(min_length=1, max_length=30)
    recommended_action: str = Field(min_length=20, max_length=6000)
    action: Literal["none", "upgrade", "mitigate", "disable_component", "shutdown_system"]
    affected_components: list[str] = Field(default_factory=list, max_length=30)
    patched_version: str | None = Field(default=None, max_length=200)
    patch_source: str | None = Field(default=None, max_length=2000)
    upstream_repositories: list[str] = Field(default_factory=list, max_length=10)

    _repositories = field_validator("upstream_repositories")(IncidentInput.validate_repositories.__func__)

    @model_validator(mode="after")
    def check_outcome(self):
        if self.patch_source:
            public_https(self.patch_source)
        if self.outcome == "fix_available" and not (self.patched_version and self.patch_source):
            raise ValueError("A fix needs a released patched version and its authoritative source")
        if self.outcome == "not_affected" and (self.action != "none" or self.affected_components):
            raise ValueError("Not affected must not carry affected components or mitigation actions")
        if self.outcome != "not_affected" and not self.affected_components:
            raise ValueError("Name the affected components")
        if self.action in {"disable_component", "shutdown_system"} and self.outcome != "action_required":
            raise ValueError("Service interruption recommendations require operator action")
        return self


class AssessmentAction(BaseModel):
    action: Literal["recheck", "resolve"]
    reason: str = Field(min_length=20, max_length=2000)
