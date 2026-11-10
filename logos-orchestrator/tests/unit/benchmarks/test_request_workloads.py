import copy

import pytest

from logos.benchmarks.workloads import build_snapshot, replay_request, request_body


def source(**updates):
    return {
        "id": 1,
        "model_id": 2,
        "model_name": "model",
        "privacy_level": "FULL",
        "result_status": "success",
        "environment": "",
        "input_payload": {
            "model": "alias",
            "messages": [{"role": "user", "content": "Synthetic example"}],
            "temperature": 0.2,
            "stream": False,
            "user": "private-user",
            "headers": {"Authorization": "secret"},
        },
        **updates,
    }


def test_snapshot_is_private_deterministic_and_keeps_source_limits():
    row = source()
    before = copy.deepcopy(row)
    first = build_snapshot([row], "Trial", "synthetic")
    body = first["requests"][0]["body"]
    assert body == {"messages": row["input_payload"]["messages"], "temperature": 0.2}
    assert row == before
    assert build_snapshot([row], "Renamed", "synthetic")["id"] == first["id"]
    row["input_payload"]["max_completion_tokens"] = 4096
    second = build_snapshot([row], "Trial", "synthetic")
    assert second["id"] != first["id"]
    assert second["requests"][0]["body"]["max_completion_tokens"] == 4096


@pytest.mark.parametrize(
    "update",
    [
        {"privacy_level": "BILLING"},
        {"result_status": "failed"},
        {"environment": "model-provider-benchmark"},
        {"model_id": None},
    ],
)
def test_selection_fails_closed_for_unavailable_or_ineligible_sources(update):
    with pytest.raises(ValueError):
        build_snapshot([source(**update)], "Trial", "synthetic")


def test_model_mixing_duplicates_and_order_are_explicit():
    with pytest.raises(ValueError):
        build_snapshot([source(), source(id=2, model_id=3)], "Trial", "synthetic")
    with pytest.raises(ValueError):
        build_snapshot([source(), source()], "Trial", "synthetic")
    rows = [source(), source(id=2)]
    assert build_snapshot(rows, "Trial", "synthetic")["id"] != build_snapshot(rows[::-1], "Trial", "synthetic")["id"]


def test_tool_history_is_preserved_and_unknown_semantics_are_rejected():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call1", "content": "Result"},
        ],
        "tools": [],
        "tool_choice": "auto",
    }
    assert request_body(body) == body
    with pytest.raises(ValueError):
        request_body({**body, "n": 2})
    with pytest.raises(ValueError):
        request_body({"messages": [{"role": "user", "content": [{"type": "image_url"}]}]})


def test_messages_replay_preserves_system_thinking_and_client_tool_history():
    body = {
        "model": "claude",
        "system": [{"type": "text", "text": "Synthetic system instruction"}],
        "thinking": {"type": "adaptive"},
        "max_tokens": 8192,
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "one", "name": "lookup", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "one", "content": "Saved result"}]},
        ],
        "metadata": {"user_id": "private"},
    }
    replay = replay_request(body)
    assert replay["operation"] == "messages"
    assert replay["body"] == {key: value for key, value in body.items() if key not in {"model", "metadata"}}
    assert "model" in body


@pytest.mark.parametrize(
    "change",
    [
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {"mcp_servers": [{"url": "https://example.invalid"}]},
        {"tools": 3},
        {"messages": 3},
    ],
)
def test_messages_replay_rejects_server_side_tools_and_malformed_inputs(change):
    with pytest.raises(ValueError):
        replay_request({"system": "Synthetic system", "messages": [{"role": "user", "content": "Test"}], **change})
