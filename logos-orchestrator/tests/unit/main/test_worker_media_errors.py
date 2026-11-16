"""Worker media policy errors retain their status and do not trigger lane retries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.main.test_effort_rejection_retry import _context, _FakeWriteQueueFactory, _scheduling
from tests.unit.main.test_request_logging import _make_dummy_db, _make_pipeline
from tests.unit.main.test_worker_stream_cancellation import PROVIDER_ID, _registry_with_session, _sent_stream_cmd_id

import logos as main
from logos.errors import UpstreamStreamError

ERROR_BODY = {
    "error": {
        "message": "Worker media must use base64 data: URLs or HTTP(S) URLs on explicitly allowed media domains.",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_media_url",
    }
}
PAYLOAD = {
    "model": "test-model",
    "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://127.0.0.1/"}}]}],
}


async def test_registry_preserves_worker_media_rejection():
    registry, websocket = _registry_with_session()
    stream = registry.send_stream_command(PROVIDER_ID, "infer_stream", {"lane_id": "lane-a"})
    consumer = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    cmd_id = _sent_stream_cmd_id(websocket)
    await registry.on_stream_end(
        PROVIDER_ID,
        {
            "cmd_id": cmd_id,
            "success": False,
            "status_code": 400,
            "body": ERROR_BODY,
            "error": ERROR_BODY["error"]["message"],
        },
    )
    with pytest.raises(UpstreamStreamError) as exc:
        await consumer
    assert exc.value.status_code == 400
    assert exc.value.body == ERROR_BODY
    assert websocket.cancel_frames() == []
    assert cmd_id not in registry._sessions[PROVIDER_ID].pending_streams


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(main, "DBManager", _make_dummy_db())
    monkeypatch.setattr(
        main, "_context_resolver", SimpleNamespace(prepare_headers_and_payload=lambda c, p: ({}, p)), raising=False
    )
    pipeline, completions, releases = _make_pipeline()
    monkeypatch.setattr(main, "_pipeline", pipeline, raising=False)
    monkeypatch.setattr(main, "write_queue", _FakeWriteQueueFactory(), raising=False)
    return completions, releases


async def test_sync_media_rejection_reaches_client_as_400(monkeypatch, pipeline):
    send = AsyncMock(
        return_value={"status_code": 400, "body": ERROR_BODY, "headers": {"content-type": "application/json"}}
    )
    monkeypatch.setattr(main, "_logosnode_registry", SimpleNamespace(send_command=send))
    response = await main._sync_response(_context("logosnode"), PAYLOAD, 42, 12, 27, -1, {}, _scheduling("logosnode"))
    assert response.status_code == 400
    assert json.loads(response.body) == ERROR_BODY
    send.assert_awaited_once()
    assert pipeline[0][-1]["result_status"] == "error"


@pytest.mark.parametrize("path", ["v1/chat/completions", "v1/responses"])
async def test_stream_media_rejection_is_invalid_request_without_retry(monkeypatch, pipeline, path):
    sent = []
    select = AsyncMock()

    async def send(**kwargs):
        sent.append(kwargs)
        raise UpstreamStreamError(400, ERROR_BODY)
        yield  # This function implements the streaming iterator protocol.

    monkeypatch.setattr(
        main, "_logosnode_registry", SimpleNamespace(send_stream_command=send, select_lane_for_model=select)
    )
    response = await main._streaming_response(
        _context("logosnode"), PAYLOAD, 42, 12, 27, -1, {}, _scheduling("logosnode")
    )
    with pytest.raises(UpstreamStreamError) as exc:
        _ = [chunk async for chunk in response.body_iterator]
    frames = main._instream_error_frames(path, exc.value)
    body = b"".join(frames)
    assert b'"type": "invalid_request_error"' in body
    assert b'"code": "invalid_media_url"' in body
    assert ERROR_BODY["error"]["message"].encode() in body
    assert len(sent) == 1
    select.assert_not_awaited()
    assert pipeline[0][-1]["result_status"] == "error"
    assert len(pipeline[1]) == 1
