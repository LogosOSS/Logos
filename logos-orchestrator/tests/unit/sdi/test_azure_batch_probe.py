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
        self.ids = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_provider_model_deployments(self, provider_id):
        return [{"model_id": 25, "model_name": "gpt-4.1"}, {"model_id": 92, "model_name": "gpt-5.6-luna"}]

    def get_batch_model_support(self, provider_id):
        return {}

    def record_model_batch_probe(
        self,
        provider_id,
        model_id,
        eligible,
        batch_deployment=None,
        detail="",
        batch_deployment_ids=None,
        checked_at=None,
    ):
        self.records.append((provider_id, model_id, eligible, batch_deployment))
        self.ids.append(list(batch_deployment_ids or []))


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


def test_a_positive_answer_expires_when_its_batch_deployment_is_gone():
    now = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)
    known = {
        "source": "probe",
        "eligible": True,
        "batch_deployment": "gpt-41-batch",
        "batch_deployment_ids": ["gpt-41-batch"],
        "checked_at": now - datetime.timedelta(hours=1),
    }
    old = (now - datetime.timedelta(days=2)).timestamp()

    assert not needs_probe(known, [{"id": "gpt-41", "updated_at": old}, {"id": "gpt-41-batch", "updated_at": old}], now)
    assert needs_probe(known, [{"id": "gpt-41", "updated_at": old}], now)


async def test_every_batch_deployment_of_a_model_is_recorded(monkeypatch):
    db = _ProbeDB()
    db.get_provider_model_deployments = lambda provider_id: [{"model_id": 25, "model_name": "gpt-4.1"}]
    monkeypatch.setitem(probe_provider.__globals__, "DBManager", lambda: db)
    calls = []
    original = probe_provider.__globals__["probe_deployment"]

    async def fake_probe(provider, deployment_id, operation, client):
        calls.append(deployment_id)
        return (deployment_id != "gpt-41-std"), "probe"

    monkeypatch.setitem(probe_provider.__globals__, "probe_deployment", fake_probe)
    raw = [
        {"id": "a-batch", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-4.1", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-41-std", "model": "gpt-4.1", "status": "succeeded"},
    ]
    async with httpx.AsyncClient() as client:
        await probe_provider(PROVIDER, raw, client)

    assert sorted(calls) == ["a-batch", "gpt-4.1", "gpt-41-std"]
    assert db.records == [(8, 25, True, "a-batch")]
    assert db.ids == [["a-batch", "gpt-4.1"]]
    # Both batch deployments stay out of interactive routing.
    planned = plan_sync("https://res.openai.azure.com", raw, set(db.ids[0]))
    assert "/deployments/gpt-41-std/" in planned[0]["endpoint"]
    assert original is not fake_probe


class _SyncDB:
    def __init__(self):
        self.support = {}
        self.upserts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_batch_model_support(self, provider_id):
        return self.support

    def sync_azure_deployments(self, provider_id, planned):
        self.upserts.append(planned)
        return {"changed": True, "new_models": []}


async def test_a_batch_deployment_found_by_the_probe_leaves_interactive_routing_in_the_same_pass():
    from logos.sdi.azure_deployment_sync import AzureDeploymentSyncService

    db = _SyncDB()
    raw = [
        {"id": "gpt-4.1", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-41-std", "model": "gpt-4.1", "status": "succeeded"},
    ]

    async def fetch(host, api_key, client):
        return raw

    async def probe(provider, deployments, client):
        db.support = {25: {"eligible": True, "batch_deployment": "gpt-4.1", "batch_deployment_ids": ["gpt-4.1"]}}
        return 1

    async def no_notifications(db):
        return None

    sync = AzureDeploymentSyncService(enabled=False)
    namespace = AzureDeploymentSyncService._sync_provider.__globals__
    patches = {
        "fetch_azure_deployments": fetch,
        "probe_provider": probe,
        "deliver_discovery_notifications": no_notifications,
        "DBManager": lambda: db,
        "BATCH_PROBE_ENABLED": True,
    }
    saved = {name: namespace[name] for name in patches}
    namespace.update(patches)
    try:
        async with httpx.AsyncClient() as client:
            changed, _ = await sync._sync_provider({**PROVIDER, "id": 8}, client)
    finally:
        namespace.update(saved)

    assert changed
    assert len(db.upserts) == 2
    assert "/deployments/gpt-4.1/" in db.upserts[0][0]["endpoint"]
    assert "/deployments/gpt-41-std/" in db.upserts[1][0]["endpoint"]


async def test_a_provider_row_as_the_sync_reads_it_is_probed_on_the_azure_batch_surface(monkeypatch):
    # get_azure_providers rows: the probe must address the resource-level
    # Azure Batch API with the api-key header, whatever else the row carries.
    db = _ProbeDB()
    db.get_provider_model_deployments = lambda provider_id: [{"model_id": 25, "model_name": "gpt-4.1"}]
    monkeypatch.setitem(probe_provider.__globals__, "DBManager", lambda: db)
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("api-key"), request.headers.get("authorization")))
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"id": "file-probe"})
        if request.url.path.endswith("/batches"):
            return httpx.Response(400, json=SKU_REFUSAL)
        return httpx.Response(200, json={})

    row = {"id": 8, "name": "azure", "base_url": PROVIDER["base_url"], "api_key": "az-key"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await probe_provider(row, [{"id": "gpt-41", "model": "gpt-4.1", "status": "succeeded"}], client)

    assert seen[0] == ("https://res.openai.azure.com/openai/v1/files", "az-key", None)
    assert db.records == [(8, 25, False, None)]


async def test_a_timed_out_reprobe_keeps_a_confirmed_batch_deployment(monkeypatch):
    db = _ProbeDB()
    db.get_provider_model_deployments = lambda provider_id: [{"model_id": 25, "model_name": "gpt-4.1"}]
    db.get_batch_model_support = lambda provider_id: {
        25: {
            "eligible": True,
            "batch_deployment": "a-batch",
            "batch_deployment_ids": ["a-batch", "gpt-4.1"],
            "source": "probe",
            "checked_at": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        }
    }
    checked = []
    original_record = db.record_model_batch_probe

    def record(*args, checked_at=None, **kwargs):
        checked.append(checked_at)
        return original_record(*args, **kwargs)

    db.record_model_batch_probe = record
    monkeypatch.setitem(probe_provider.__globals__, "DBManager", lambda: db)

    async def fake_probe(provider, deployment_id, operation, client):
        return {"a-batch": (True, "accepted"), "gpt-4.1": (None, "timeout"), "gpt-41-std": (False, "sku")}[
            deployment_id
        ]

    monkeypatch.setitem(probe_provider.__globals__, "probe_deployment", fake_probe)
    raw = [
        {"id": "a-batch", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-4.1", "model": "gpt-4.1", "status": "succeeded"},
        {"id": "gpt-41-std", "model": "gpt-4.1", "status": "succeeded"},
    ]
    async with httpx.AsyncClient() as client:
        await probe_provider(PROVIDER, raw, client)

    assert db.records == [(8, 25, True, "a-batch")]
    assert db.ids == [["a-batch", "gpt-4.1"]]
    # Still due for the next sync because one deployment gave no answer.
    assert checked[0] is not None
    assert needs_probe(
        {"source": "probe", "eligible": True, "batch_deployment_ids": db.ids[0], "checked_at": checked[0]},
        raw,
        datetime.datetime.now(datetime.timezone.utc),
    )
    planned = plan_sync("https://res.openai.azure.com", raw, set(db.ids[0]))
    assert "/deployments/gpt-41-std/" in planned[0]["endpoint"]
