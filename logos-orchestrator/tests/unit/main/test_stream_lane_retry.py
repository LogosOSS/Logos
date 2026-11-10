"""A pre-token retry resolves the current lane instead of reusing an evicted one."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.main.test_effort_rejection_retry import ANSWER_CHUNKS, PAYLOAD, _context, _scheduling
from tests.unit.main.test_request_logging import _make_dummy_db, _make_pipeline, _read_stream_response

import logos as main
from logos.logosnode_registry import LogosNodeCommandError


@pytest.mark.asyncio
@pytest.mark.parametrize("partial,available", [(False, True), (True, True), (False, False)])
async def test_retry_selects_current_lane_only_before_any_output(monkeypatch, partial, available):
    monkeypatch.setattr(main, "DBManager", _make_dummy_db())
    monkeypatch.setattr(
        main, "_context_resolver", SimpleNamespace(prepare_headers_and_payload=lambda c, p: ({}, p)), raising=False
    )
    monkeypatch.setattr(main, "_LOGOSNODE_PRETOKEN_RETRY_BACKOFF_S", 0)
    pipeline, completions, releases = _make_pipeline()
    monkeypatch.setattr(main, "_pipeline", pipeline, raising=False)
    sent = []
    select = AsyncMock(return_value={"lane_id": "lane-2"} if available else None)

    async def send(**kwargs):
        lane = kwargs["params"]["lane_id"]
        sent.append(lane)
        if lane == "lane-1":
            if partial:
                yield ANSWER_CHUNKS[0]
            raise LogosNodeCommandError("Lane 'lane-1' is not routable (runtime_state=sleeping)")
        for chunk in ANSWER_CHUNKS:
            yield chunk

    monkeypatch.setattr(
        main,
        "_logosnode_registry",
        SimpleNamespace(send_stream_command=send, select_lane_for_model=select),
        raising=False,
    )
    response = await main._streaming_response(
        _context("logosnode"), dict(PAYLOAD), 42, 12, 27, -1, {}, _scheduling("logosnode")
    )
    if partial or not available:
        with pytest.raises(LogosNodeCommandError):
            await _read_stream_response(response)
        assert sent == ["lane-1"]
        if partial:
            select.assert_not_awaited()
        else:
            assert select.await_count == main._LOGOSNODE_PRETOKEN_RETRIES
            assert [c["result_status"] for c in completions] == ["error"]
    else:
        body = await _read_stream_response(response)
        assert "hello" in body
        assert sent == ["lane-1", "lane-2"]
        select.assert_awaited_once_with(12, PAYLOAD["model"], request_id="req-effort")
        assert [c["result_status"] for c in completions] == ["success"]
    assert releases == [(27, 12, "logosnode", "req-effort")]
