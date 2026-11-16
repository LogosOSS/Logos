import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from logos.benchmarks import dev_nightly_pilot, optimization
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


def test_dev_pilot_plans_50_distinct_contextual_requests_with_natural_outputs():
    workload = dev_nightly_pilot.build_workload(1)
    assert workload == dev_nightly_pilot.build_workload(1)
    bodies = [request["body"] for request in workload["requests"]]
    assert len({json.dumps(body["messages"]) for body in bodies}) == workload["request_count"] == 50
    assert all("max_tokens" not in body and "ignore_eos" not in body and body["temperature"] == 0.2 for body in bodies)
    assert all(body["repetition_penalty"] == 1.05 for body in bodies)
    assert all(body["messages"][-1]["role"] == "user" for body in bodies)
    assert all(len(body["messages"][1]["content"]) > 2500 for body in bodies)
    assert all("```" in body["messages"][1]["content"] for body in bodies)
    assert sum(any(message["role"] == "assistant" for message in body["messages"]) for body in bodies) == 40
    current = {"max_num_seqs": 5, "max_num_batched_tokens": 2304}
    plan = optimization.plan(current, optimization.context(current, {}, workload, {}))
    assert len(plan) > 1
    assert all(run.samples == 50 and run.workload_id == workload["id"] for run in plan)


@pytest.mark.parametrize("state", ["previous", "fixed", "natural", "installed", "changed", "disabled"])
def test_dev_pilot_replaces_only_the_enabled_previous_workload(monkeypatch, state):
    pilot = dev_nightly_pilot
    monkeypatch.delenv("LOGOS_NIGHTLY_REQUEST_USERNAME", raising=False)
    snapshot = pilot.build_workload(1)
    previous_id, fixed_id, natural_id = sorted(pilot.PREVIOUS_WORKLOAD_IDS)
    selected_id = {
        "installed": snapshot["id"],
        "changed": "another-workload",
        "fixed": fixed_id,
        "natural": natural_id,
    }.get(state, previous_id)
    db_manager = MagicMock()
    session = db_manager.return_value.__enter__.return_value.session
    session.execute.return_value.one.return_value = SimpleNamespace(pair_id=2, model_id=1)
    monkeypatch.setattr(pilot, "DBManager", db_manager)
    monkeypatch.setattr(
        pilot.nightly_store,
        "status",
        Mock(return_value={"enabled": state != "disabled", "workload_id": selected_id, "status": "scheduled"}),
    )
    update = Mock(return_value={"enabled": True, "workload_id": snapshot["id"], "status": "scheduled"})
    monkeypatch.setattr(pilot.nightly_store, "set_enabled", update)
    load = Mock(return_value=snapshot)
    monkeypatch.setattr(pilot.workloads, "load", load)

    if state in {"changed", "disabled"}:
        with pytest.raises(RuntimeError, match="expected enabled Dev pilot"):
            pilot.install()
        session.commit.assert_not_called()
        load.assert_not_called()
    else:
        pilot.install()
        session.commit.assert_called_once()
        load.assert_called_once_with(snapshot["id"], 1)
        insert_parameters = session.execute.call_args.args[1]
        assert insert_parameters["request_count"] == 50 and insert_parameters["id"] == snapshot["id"]
    if state in {"previous", "fixed", "natural"}:
        update.assert_called_once_with(
            2, True, expected=True, workload_id=snapshot["id"], expected_workload_id=selected_id
        )
    else:
        update.assert_not_called()


def test_dev_pilot_cannot_replace_automatic_request_selection(monkeypatch):
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "configured-user")
    db_manager = Mock()
    monkeypatch.setattr(dev_nightly_pilot, "DBManager", db_manager)
    with pytest.raises(RuntimeError, match="manually selected nightly requests"):
        dev_nightly_pilot.install()
    db_manager.assert_not_called()


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
