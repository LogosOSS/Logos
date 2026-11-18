"""Bounded ingestion of critical reviewed advisories and known exploited CVEs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import text

from . import db, github, security_store
from .security_models import IncidentInput

CISA_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


async def github_advisories(since: datetime) -> None:
    params = {
        "severity": "critical",
        "type": "reviewed",
        "modified": f">={since.isoformat()}",
        "per_page": 100,
        "sort": "updated",
        "direction": "asc",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(20):
            response = await client.get("https://api.github.com/advisories", params=params, headers=github._headers())
            response.raise_for_status()
            for item in response.json():
                if item.get("withdrawn_at") or item.get("severity") != "critical":
                    continue
                body = IncidentInput(
                    advisory_id=item.get("cve_id") or item["ghsa_id"],
                    title=item["summary"],
                    severity="critical",
                    source_url=item["html_url"],
                    summary=(item.get("description") or item["summary"])[:16000],
                    importance="GitHub-reviewed critical vulnerability; assess repository exposure before acting.",
                )
                await security_store.ingest(
                    body,
                    "feed:github",
                    {
                        "ghsa_id": item["ghsa_id"],
                        "vulnerabilities": item.get("vulnerabilities", []),
                        "references": item.get("references", [])[:30],
                        "withdrawn_at": item.get("withdrawn_at"),
                    },
                )
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return
            # Follow only the cursor, never an untrusted host with the bot token.
            cursor = parse_qs(urlsplit(next_url).query).get("after")
            if not cursor:
                raise ValueError("Advisory feed pagination has no cursor")
            params["after"] = cursor[0]
    raise ValueError("Advisory feed exceeded 20 pages; cursor not advanced")


async def cisa_advisories(since: datetime) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(CISA_URL)
        response.raise_for_status()
        if len(response.content) > 20_000_000:
            raise ValueError("CISA feed exceeds size limit")
        for item in response.json()["vulnerabilities"]:
            if str(item["dateAdded"]) < since.date().isoformat():
                continue
            body = IncidentInput(
                advisory_id=item["cveID"],
                title=item["vulnerabilityName"][:300],
                severity="high",
                known_exploited=True,
                source_url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
                summary=(item["shortDescription"] + "\nRequired action: " + item["requiredAction"])[:16000],
                importance="CISA confirms exploitation in the wild. Determine exposure and prioritize containment.",
            )
            incident = await security_store.ingest(body, "feed:cisa", item)
            # A CVE may already exist from GitHub or manual intake. Keep that
            # record and attach confirmed exploitation without replacing it.
            async with db.sessionmaker()() as conn:
                await conn.execute(
                    text("""
                    UPDATE security_incidents SET known_exploited=true,
                        source_data=source_data || CAST(:evidence AS jsonb),
                        revision=revision+1, updated_at=now()
                    WHERE id=:id AND (NOT known_exploited OR source_data->'cisa' IS DISTINCT FROM CAST(:item AS jsonb))
                """),
                    {"id": incident["id"], "evidence": json.dumps({"cisa": item}), "item": json.dumps(item)},
                )
                await conn.commit()


async def poll_feeds() -> None:
    now = datetime.now(timezone.utc)
    for name, ingest in (("github", github_advisories), ("cisa", cisa_advisories)):
        async with db.sessionmaker()() as conn:
            row = (
                (await conn.execute(text("SELECT * FROM security_feed_state WHERE name=:name"), {"name": name}))
                .mappings()
                .first()
            )
        last = row["last_success"] if row else None
        if last and now - last < timedelta(hours=1):
            continue
        error = None
        try:
            # First start covers the last week, not an unbounded historic backlog.
            await ingest((last or now - timedelta(days=7)) - timedelta(hours=1))
        except Exception as exc:
            error = str(exc)[:1000]
        async with db.sessionmaker()() as conn:
            await conn.execute(
                text("""
                INSERT INTO security_feed_state (name, last_success, error) VALUES (:name, :success, :error)
                ON CONFLICT (name) DO UPDATE SET
                    last_success=COALESCE(EXCLUDED.last_success, security_feed_state.last_success), error=EXCLUDED.error
            """),
                {"name": name, "success": None if error else now, "error": error},
            )
            await conn.commit()
