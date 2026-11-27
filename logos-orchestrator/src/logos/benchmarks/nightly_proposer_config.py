"""Deployment-wide model selection for nightly benchmark suggestions."""

import os
from dataclasses import dataclass, field
from typing import Literal

import httpx
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from logos.dbutils.dbmanager import DBManager

DEFAULT_LOCAL_MODEL = "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
Provider = Literal["local", "logos_prod"]
CLOUD_CATALOG_TIMEOUT = 5.0


@dataclass(frozen=True)
class ProposerModel:
    provider: Provider
    model: str
    revision: int | None = None


@dataclass(frozen=True)
class LogosProdGateway:
    base_url: httpx.URL
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "LogosProdGateway | None":
        api_key = os.getenv("LOGOS_NIGHTLY_PROD_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            url = httpx.URL(os.getenv("LOGOS_NIGHTLY_PROD_BASE_URL", "").strip().rstrip("/"))
        except httpx.InvalidURL:
            return None
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or not url.path.endswith("/v1")
        ):
            return None
        return cls(url, api_key)

    async def request(
        self, method: str, path: str, *, body: dict | None = None, timeout: float = CLOUD_CATALOG_TIMEOUT
    ) -> dict:
        url = self.base_url.copy_with(path=f"{self.base_url.path}/{path}")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(method, url, headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected Logos PROD response")
        return payload

    async def models(self) -> list[str]:
        payload = await self.request("GET", "models")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Unexpected Logos PROD model catalog")
        return sorted(
            {
                row["id"]
                for row in data
                if isinstance(row, dict) and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 255
            },
            key=str.casefold,
        )


def selection() -> ProposerModel:
    """Use the saved selection, then the deployment default."""
    with DBManager() as db:
        row = db.session.execute(
            text("SELECT provider, model, revision FROM nightly_proposer_model WHERE singleton")
        ).one_or_none()
    if row:
        return ProposerModel(row.provider, row.model, row.revision)
    cloud_default = os.getenv("LOGOS_NIGHTLY_PROD_DEFAULT_MODEL", "").strip()
    if cloud_default:
        return ProposerModel("logos_prod", cloud_default)
    return ProposerModel("local", DEFAULT_LOCAL_MODEL)


def save_selection(provider: Provider, model: str, revision: int | None) -> ProposerModel | None:
    """Save atomically if the admin edited the current revision."""
    with DBManager() as db:
        query = (
            "INSERT INTO nightly_proposer_model (provider, model) VALUES (:provider, :model) "
            "ON CONFLICT (singleton) DO NOTHING RETURNING revision"
            if revision is None
            else "UPDATE nightly_proposer_model SET provider = :provider, model = :model, revision = revision + 1 "
            "WHERE singleton AND revision = :revision RETURNING revision"
        )
        saved_revision = db.session.execute(
            text(query), {"provider": provider, "model": model, "revision": revision}
        ).scalar_one_or_none()
        db.session.commit()
    return ProposerModel(provider, model, saved_revision) if saved_revision is not None else None


def local_models() -> list[str]:
    """List models the agent identity may use on local workers."""
    with DBManager() as db:
        key_id = db.get_agent_api_key_id()
        deployments = db.get_deployments_for_api_key(key_id) if key_id is not None else []
    return sorted({row["model_name"] for row in deployments if row["type"] == "logosnode"}, key=str.casefold)


async def catalog(provider: Provider) -> list[str]:
    """List selectable models using the same credentials as proposal inference."""
    if provider == "local":
        return await run_in_threadpool(local_models)
    gateway = LogosProdGateway.from_env()
    if gateway is None:
        raise ValueError("Logos PROD is not configured")
    return await gateway.models()
