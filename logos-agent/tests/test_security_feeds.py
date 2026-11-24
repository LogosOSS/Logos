"""Feed cursors and filtering preserve actionable, unfixed incidents."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from app import security_feeds as feeds


@pytest.mark.parametrize(
    "since, expected",
    [
        ("2026-11-16T22:59:34.318770+00:00", ">=2026-11-16T22:59:34+00:00"),
        ("2026-11-16T22:59:34.999999+00:00", ">=2026-11-16T22:59:34+00:00"),
        ("2026-11-17T00:59:34.000001+02:00", ">=2026-11-17T00:59:34+02:00"),
        ("2026-11-16T22:59:34+00:00", ">=2026-11-16T22:59:34+00:00"),
    ],
)
async def test_github_search_date_truncates_seconds_and_accepts_empty_results(monkeypatch, since, expected):
    monkeypatch.setattr(feeds.github, "_headers", lambda: {})

    def handle(request):
        assert request.url.params["modified"] == expected
        return httpx.Response(200, json=[])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feeds.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    monkeypatch.setattr(feeds.security_store, "ingest", AsyncMock())
    await feeds.github_advisories(datetime.fromisoformat(since))
    feeds.security_store.ingest.assert_not_awaited()


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
    await feeds.github_advisories(datetime(2026, 11, 16, 22, 59, 34, 318770, tzinfo=timezone.utc))
    assert len(seen) == 2
    assert all(r.url.host == "api.github.com" for r in seen)
    assert all(r.url.params["modified"] == ">=2026-11-16T22:59:34+00:00" for r in seen)
    assert seen[1].url.params["after"] == "next"
    assert ingest.await_count == 1
    assert ingest.call_args.args[2]["vulnerabilities"][0]["first_patched_version"] is None


async def test_long_github_title_does_not_block_following_advisories(monkeypatch):
    monkeypatch.setattr(feeds.github, "_headers", lambda: {"Authorization": "Bearer test-token"})
    advisory = {
        "ghsa_id": "GHSA-2345-6789-abcd",
        "cve_id": "CVE-2026-12345",
        "severity": "critical",
        "summary": "Critical parser vulnerability " * 20,
        "description": "Untrusted input can execute remote code.",
        "html_url": "https://github.com/advisories/GHSA-2345-6789-abcd",
    }
    following = {**advisory, "cve_id": "CVE-2026-12346", "summary": "Another critical parser vulnerability"}
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feeds.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[advisory, following])), **kwargs
        ),
    )
    ingest = AsyncMock()
    monkeypatch.setattr(feeds.security_store, "ingest", ingest)
    await feeds.github_advisories(datetime.now(timezone.utc))
    first, second = [call.args[0] for call in ingest.await_args_list]
    assert first.title == advisory["summary"][:300]
    assert first.advisory_id == "CVE-2026-12345"
    assert second.title == following["summary"]
    assert second.advisory_id == "CVE-2026-12346"


@pytest.mark.parametrize(
    "next_url, expected_calls",
    [("https://api.github.com/advisories?page=2", 1), ("https://api.github.com/advisories?after=next", 20)],
)
async def test_incomplete_github_pagination_is_a_failure(monkeypatch, next_url, expected_calls):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=[], headers={"Link": f'<{next_url}>; rel="next"'})

    monkeypatch.setattr(feeds.github, "_headers", lambda: {})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feeds.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    with pytest.raises(ValueError, match="pagination has no cursor|exceeded 20 pages"):
        await feeds.github_advisories(datetime(2026, 11, 16, 22, 59, 34, 318770, tzinfo=timezone.utc))
    assert len(calls) == expected_calls


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
        if "since" in params:
            self.since = params["since"]
        return self

    def scalar_one(self):
        return self.since

    def mappings(self):
        return self

    def first(self):
        return self.previous

    async def commit(self):
        pass


@pytest.mark.parametrize("message", ["upstream unavailable", ""])
async def test_failed_feed_does_not_advance_durable_cursor(monkeypatch, message):
    previous = datetime.now(timezone.utc) - timedelta(hours=2)
    conn = Connection({"last_success": previous})
    monkeypatch.setattr(feeds.db, "sessionmaker", lambda: lambda: conn)
    monkeypatch.setattr(feeds, "github_advisories", AsyncMock(side_effect=RuntimeError(message)))
    monkeypatch.setattr(feeds, "cisa_advisories", AsyncMock(side_effect=RuntimeError(message)))
    await feeds.poll_feeds()
    writes = [params for sql, params in conn.writes if "success" in params]
    assert len(writes) == 2
    assert all(w["success"] is None and w["error"] == (message or "RuntimeError") for w in writes)
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
