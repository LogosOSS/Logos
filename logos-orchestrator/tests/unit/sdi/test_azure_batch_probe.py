"""Probing Azure deployments for Batch support before any batch is submitted."""

import datetime
import json

import httpx

from logos.sdi import azure_batch_probe
from logos.sdi.azure_batch_probe import needs_probe, probe_deployment, probe_line, probe_provider
from logos.sdi.azure_deployment_sync import plan_sync

PROVIDER = {
    "id": 8,
    "name": "azure",
    "base_url": "https://res.openai.azure.com/openai/deployments/",
    "cloud_provider_type": "azure",
    "api_key": "az-key",
}

SKU_REFUSAL = {
    "errors": {
        "data": [
            {
                "code": "invalid_deployment_type",
                "message": "The current deployment 'gpt-5.6-luna' is using the SKU 'GlobalStandard', which is not "
                "supported for batch jobs.",
            }
        ],
        "object": "list",
    }
}


def _client(handler, seen):
    def record(request):
        seen.append((request.method, request.url.path))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(record))


def _upstream(creation):
    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/files"):
            return httpx.Response(200, json={"id": "file-probe"})
        if request.method == "POST" and request.url.path.endswith("/batches"):
            return creation(request)
        return httpx.Response(200, json={})

    return handler


def test_the_probe_line_addresses_the_deployment_on_its_operation():
    line = json.loads(probe_line("gpt-41-batch", "chat/completions"))
    assert line["url"] == "/chat/completions"
    assert line["body"]["model"] == "gpt-41-batch"
    assert probe_line("img", "images/generations") is None


async def test_a_standard_deployment_is_refused_and_the_upload_is_removed():
    seen = []
    async with _client(_upstream(lambda request: httpx.Response(400, json=SKU_REFUSAL)), seen) as client:
        answer, detail = await probe_deployment(PROVIDER, "gpt-5.6-luna", "responses", client)

    assert answer is False
    assert "GlobalStandard" in detail
    assert ("DELETE", "/openai/v1/files/file-probe") in seen
    assert not any(path.endswith("/cancel") for _, path in seen)


async def test_an_accepted_probe_batch_is_cancelled_at_once():
    seen = []
    accepted = _upstream(lambda request: httpx.Response(200, json={"id": "batch_probe", "status": "validating"}))
    async with _client(accepted, seen) as client:
        answer, _ = await probe_deployment(PROVIDER, "gpt-41-batch", "chat/completions", client)

    assert answer is True
    assert ("POST", "/openai/v1/batches/batch_probe/cancel") in seen
    assert ("DELETE", "/openai/v1/files/file-probe") in seen


async def test_an_unrelated_refusal_is_inconclusive():
    seen = []
    quota = _upstream(lambda request: httpx.Response(429, json={"error": {"message": "Token quota exceeded"}}))
    async with _client(quota, seen) as client:
        answer, detail = await probe_deployment(PROVIDER, "gpt-41", "chat/completions", client)

    assert answer is None
    assert "quota" in detail


async def test_an_operation_batch_does_not_run_is_not_batchable_without_a_request():
    seen = []
    async with _client(lambda request: httpx.Response(500), seen) as client:
        answer, _ = await probe_deployment(PROVIDER, "img", "images/generations", client)

    assert answer is False
    assert seen == []


def test_a_model_is_probed_again_when_its_deployment_changed_or_the_answer_is_old():
    now = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)
    fresh = {"source": "probe", "checked_at": now - datetime.timedelta(days=1)}
    deployment = {"updated_at": (now - datetime.timedelta(days=2)).timestamp()}

    assert needs_probe(None, [deployment], now)
    assert needs_probe({"source": "refusal", "checked_at": now}, [deployment], now)
    assert not needs_probe(fresh, [deployment], now)
    assert needs_probe(fresh, [{"updated_at": now.timestamp()}], now)
    assert needs_probe({"source": "probe", "checked_at": now - datetime.timedelta(days=30)}, [deployment], now)


def test_a_batch_deployment_is_not_chosen_for_interactive_traffic():
    deployments = [
        {"id": "gpt-4.1", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-4.1-std", "model": "gpt-4.1", "status": "succeeded"},
    ]
    planned = plan_sync("https://res.openai.azure.com", deployments, {"gpt-4.1"})
    assert "/deployments/gpt-4.1-std/" in planned[0]["endpoint"]
    # Without the probe's knowledge the name match wins, as before.
    assert "/deployments/gpt-4.1/" in plan_sync("https://res.openai.azure.com", deployments)[0]["endpoint"]


class _ProbeDB:
    def __init__(self):
        self.records = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_provider_model_deployments(self, provider_id):
        return [{"model_id": 25, "model_name": "gpt-4.1"}, {"model_id": 92, "model_name": "gpt-5.6-luna"}]

    def get_batch_model_support(self, provider_id):
        return {}

    def record_model_batch_probe(self, provider_id, model_id, eligible, batch_deployment=None, detail=""):
        self.records.append((provider_id, model_id, eligible, batch_deployment))


async def test_a_model_is_batchable_through_whichever_deployment_accepted(monkeypatch):
    db = _ProbeDB()
    monkeypatch.setitem(probe_provider.__globals__, "DBManager", lambda: db)

    def creation(request):
        model = json.loads(_uploaded["content"])["body"]["model"]
        if model == "gpt-41-batch":
            return httpx.Response(200, json={"id": "batch_probe"})
        return httpx.Response(400, json=SKU_REFUSAL)

    _uploaded = {}

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/files"):
            body = request.content.decode()
            _uploaded["content"] = next(line for line in body.splitlines() if line.startswith("{"))
            return httpx.Response(200, json={"id": "file-probe"})
        if request.method == "POST" and request.url.path.endswith("/batches"):
            return creation(request)
        return httpx.Response(200, json={})

    raw = [
        {"id": "gpt-41", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-41-batch", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna", "status": "succeeded"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probed = await probe_provider(PROVIDER, raw, client)

    assert probed == 2
    assert sorted(db.records) == [(8, 25, True, "gpt-41-batch"), (8, 92, False, None)]
    assert azure_batch_probe.PROBE_CUSTOM_ID in _uploaded["content"]
