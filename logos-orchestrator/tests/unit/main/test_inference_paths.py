"""The public inference mirrors reject unsupported paths before authentication."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

import logos as main
from logos.inference_paths import ALLOWED_INFERENCE_PATHS
from logos.pipeline.context_resolver import ContextResolver
from logos.routers import user_facing

MIRRORS = ("v1", "openai", "jobs/v1", "jobs/openai", "v2", "jobs/v2")
SUPPORTED_MIRRORS = [
    (mirror, path)
    for mirror in MIRRORS
    for path in sorted(ALLOWED_INFERENCE_PATHS)
    if path.startswith("v2/" if "v2" in mirror else "v1/")
]


@pytest.fixture
def forwarding(monkeypatch):
    forward = AsyncMock(return_value=JSONResponse({"forwarded": True}))
    monkeypatch.setattr(user_facing, "handle_sync_request", forward)
    monkeypatch.setattr(user_facing, "submit_job_request", forward)
    auth = Mock(side_effect=AssertionError("Unexpected authentication"))
    monkeypatch.setattr(user_facing, "authenticate_api_key", auth)
    monkeypatch.setattr(main, "auth_parse_log", auth)
    return forward


@pytest.mark.parametrize("mirror,path", SUPPORTED_MIRRORS)
def test_supported_operations_reach_pipeline(mirror, path, forwarding):
    operation = path.split("/", 1)[1]
    response = TestClient(main.app).post(f"/{mirror}/{operation}", json={})
    assert response.status_code == 200
    assert forwarding.await_args.args[0] == path


@pytest.mark.parametrize("path", sorted(p for p in ALLOWED_INFERENCE_PATHS if "/" not in p))
def test_native_operations_reach_pipeline(path, forwarding):
    assert TestClient(main.app).post(f"/{path}", json={}).status_code == 200
    assert forwarding.await_args.args[0] == path


@pytest.mark.parametrize("mirror", MIRRORS)
@pytest.mark.parametrize("operation", ["unknown", "fine_tuning/jobs", "assistants", "vector_stores", "models"])
def test_unsupported_operations_do_not_reach_pipeline(mirror, operation, forwarding):
    assert TestClient(main.app).post(f"/{mirror}/{operation}", json={}).status_code == 404
    forwarding.assert_not_called()


@pytest.mark.parametrize("mirror", MIRRORS)
@pytest.mark.parametrize(
    "operation",
    [
        "chat//completions",
        "./chat/completions",
        "other/../chat/completions",
        "chat/completions/",
        "%2e/chat/completions",
        "other/%2E%2e/chat/completions",
        "chat%2fcompletions",
        "%63hat/completions/..",
        "%63hat/completions",
        "chat/%252e%252e/completions",
        "chat\\completions",
        "",
    ],
)
async def test_noncanonical_paths_are_rejected_as_received(mirror, operation, forwarding):
    # Send the ASGI scope directly: HTTP clients normalize literal dot segments
    # before dispatch, which would test the client rather than the server.
    raw = f"/{mirror}/{operation}"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": unquote(raw),
        "raw_path": raw.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("192.0.2.1", 1234),
        "server": ("test", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    await main.app(scope, receive, send)
    assert sent[0]["status"] == 404
    forwarding.assert_not_called()


@pytest.mark.parametrize("handler", [main.handle_sync_request, main.submit_job_request])
async def test_pipeline_entry_points_reject_unknown_path(handler, forwarding):
    request = Request({"type": "http", "method": "POST", "path": "/v1/unknown", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await handler("v1/unknown", request)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("path", ["v1/unknown", "v1/../completions", "v1/fine_tuning/jobs"])
def test_cloud_url_requires_inference_operation(path):
    with pytest.raises(HTTPException) as exc:
        ContextResolver._cloud_forward_url("https://provider.example/v1", path, None)
    assert exc.value.status_code == 404


def test_worker_and_orchestrator_operations_stay_consistent():
    # Read the literal without importing GPU worker runtime dependencies.
    worker = Path(__file__).resolve().parents[4] / "logos-workernode/logos_worker_node/logos_bridge.py"
    tree = ast.parse(worker.read_text())
    declaration = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_ALLOWED_REQUEST_PATHS"
    )
    assert ast.literal_eval(declaration.value.args[0]) == ALLOWED_INFERENCE_PATHS | {"v1/models"}
