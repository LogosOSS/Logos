"""Feed cursors and filtering preserve actionable, unfixed incidents."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
from app import security_feeds as feeds


async def test_github_pagination_keeps_credentials_on_github_and_excludes_noncritical(monkeypatch):
    monkeypatch.setattr(feeds.github, "_headers", lambda: {"Authorization": "Bearer test-token"})
    seen = []
    advisory = {
        "ghsa_id": "GHSA-2345-6789-abcd",
        "cve_id": "CVE-2026-12345",
        "severity": "critical",
        "summary": "Critical parser vulnerability",
        "description": "Untrusted input can execute remote code.",
        "html_url": "https://github.com/advisories/GHSA-2345-6789-abcd",
        "vulnerabilities": [{"package": {"name": "parser"}, "first_patched_version": None}],
    }

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json=[advisory, {**advisory, "severity": "medium"}],
                headers={"Link": '<https://untrusted.invalid/advisories?after=next>; rel="next"'},
            )
        return httpx.Response(200, json=[{**advisory, "withdrawn_at": "2026-11-18T00:00:00Z"}])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feeds.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    ingest = AsyncMock()
    monkeypatch.setattr(feeds.security_store, "ingest", ingest)
    await feeds.github_advisories(datetime.now(timezone.utc))
    assert len(seen) == 2
    assert all(r.url.host == "api.github.com" for r in seen)
    assert seen[1].url.params["after"] == "next"
    assert ingest.await_count == 1
    assert ingest.call_args.args[2]["vulnerabilities"][0]["first_patched_version"] is None


class Connection:
    def __init__(self, previous=None):
        self.previous = previous
        self.writes = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, query, params):
        self.writes.append((str(query), params))
        return self

    def mappings(self):
        return self

    def first(self):
        return self.previous

    async def commit(self):
        pass


async def test_failed_feed_does_not_advance_durable_cursor(monkeypatch):
    previous = datetime.now(timezone.utc) - timedelta(hours=2)
    conn = Connection({"last_success": previous})
    monkeypatch.setattr(feeds.db, "sessionmaker", lambda: lambda: conn)
    monkeypatch.setattr(feeds, "github_advisories", AsyncMock(side_effect=RuntimeError("upstream unavailable")))
    monkeypatch.setattr(feeds, "cisa_advisories", AsyncMock(side_effect=RuntimeError("upstream unavailable")))
    await feeds.poll_feeds()
    writes = [params for sql, params in conn.writes if "INSERT" in sql]
    assert len(writes) == 2
    assert all(w["success"] is None and w["error"] == "upstream unavailable" for w in writes)
    assert feeds.github_advisories.call_args.args[0] == previous - timedelta(hours=1)


async def test_kev_without_patch_is_ingested_and_enriches_an_existing_incident(monkeypatch):
    item = {
        "cveID": "CVE-2026-12345",
        "dateAdded": "2026-11-18",
        "vulnerabilityName": "Exploited parser issue",
        "shortDescription": "The parser executes untrusted code remotely.",
        "requiredAction": "Disable the exposed service until a fix is available.",
    }
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feeds.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"vulnerabilities": [item]})), **kwargs
        ),
    )
    ingest = AsyncMock(return_value={"id": 4})
    monkeypatch.setattr(feeds.security_store, "ingest", ingest)
    conn = Connection()
    monkeypatch.setattr(feeds.db, "sessionmaker", lambda: lambda: conn)
    await feeds.cisa_advisories(datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert ingest.call_args.args[0].known_exploited is True
    assert "Disable the exposed service" in ingest.call_args.args[0].summary
    assert conn.writes[0][1]["id"] == 4
    assert '"cisa"' in conn.writes[0][1]["evidence"]
