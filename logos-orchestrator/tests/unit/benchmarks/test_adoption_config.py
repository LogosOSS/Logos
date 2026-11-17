"""The winner of a nightly tuning reaches the node's central configuration."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from logos.benchmarks import adoption_config as mod
from logos.benchmarks import nightly_scheduler as scheduler_mod
from logos.benchmarks.nightly_scheduler import NightlyScheduler


def _entry(name="org/a", capability=None, overrides=None):
    return {"model_name": name, "capability": capability or {}, "overrides": overrides or {}}


def test_capability_values_stay_under_capability_and_the_rest_becomes_overrides():
    entry = _entry(
        capability={"tensor_parallel_size": 2, "kv_cache_memory_bytes": "4G", "min_context_fraction": 1},
        overrides={"max_model_len": 8192, "quantization": "awq"},
    )

    result = mod.tuned_entry(
        entry,
        {"tensor_parallel_size": 4, "kv_cache_memory_bytes": "8G", "max_num_seqs": 64, "enable_prefix_caching": False},
    )

    assert result["capability"] == {"tensor_parallel_size": 4, "kv_cache_memory_bytes": "8G", "min_context_fraction": 1}
    assert result["overrides"] == {
        "max_model_len": 8192,
        "quantization": "awq",
        "max_num_seqs": 64,
        "enable_prefix_caching": False,
    }
    assert entry["capability"]["tensor_parallel_size"] == 2


def test_values_the_winner_does_not_change_stay_as_they_are():
    entry = _entry(capability={"tensor_parallel_size": 2}, overrides={"max_num_seqs": 8})

    result = mod.tuned_entry(entry, {"attention_backend": "TRITON_ATTN"})

    assert result["capability"] == {"tensor_parallel_size": 2}
    assert result["overrides"] == {"max_num_seqs": 8, "attention_backend": "TRITON_ATTN"}


def test_a_value_the_entry_did_not_have_is_added_where_it_belongs():
    result = mod.tuned_entry(_entry(), {"tensor_parallel_size": 2, "max_num_seqs": 4})

    assert result["capability"] == {"tensor_parallel_size": 2}
    assert result["overrides"] == {"max_num_seqs": 4}


def test_the_batched_token_limit_becomes_a_flag_and_replaces_an_earlier_one():
    entry = _entry(overrides={"extra_args": ["--foo", "--max-num-batched-tokens", "1024", "--bar=1"]})

    result = mod.tuned_entry(entry, {"max_num_batched_tokens": 4096})

    assert result["overrides"]["extra_args"] == ["--foo", "--bar=1", "--max-num-batched-tokens", "4096"]
    assert "max_num_batched_tokens" not in result["overrides"]


def test_a_parameter_the_tuning_never_offers_is_refused():
    with pytest.raises(ValueError):
        mod.tuned_entry(_entry(), {"unknown_setting": 1})


class _Db:
    def __init__(self, sources):
        self.sources = sources

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_node_config_sources(self, provider_id):
        return self.sources


@pytest.fixture
def store(monkeypatch):
    saved = []
    state = SimpleNamespace(config={"revision": 3, "entries": []}, results=[], saved=saved)

    def load(provider_id):
        return state.config

    async def save(provider_id, entries, revision, updated_by, source="ui", source_by_model=None):
        saved.append((provider_id, [e.model_dump() for e in entries], revision, updated_by, source, source_by_model))
        return state.results.pop(0) if state.results else {"provider_id": provider_id, "config_revision": revision + 1}

    from logos.routers import logosnode

    monkeypatch.setattr(logosnode, "_load_node_config", load)
    monkeypatch.setattr(logosnode, "save_node_config", save)
    monkeypatch.setattr(mod, "DBManager", lambda: _Db({"org/a": "ui", "org/b": "yaml_import"}))
    return state


def _response(status, error):
    return SimpleNamespace(status_code=status, body=json.dumps({"error": error}).encode())


def test_the_winner_is_saved_with_the_source_tuning_and_the_other_sources_kept(store):
    store.config = {
        "revision": 3,
        "entries": [_entry("org/a", {"tensor_parallel_size": 2}), _entry("org/b", overrides={"quantization": "awq"})],
    }

    reason = asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 32}))

    assert reason is None
    provider_id, entries, revision, author, source, by_model = store.saved[0]
    assert (provider_id, revision, author, source) == (7, 3, None, "tuning")
    assert entries[0]["overrides"] == {"max_num_seqs": 32}
    assert entries[1]["overrides"] == {"quantization": "awq"}
    assert by_model == {"org/a": "tuning", "org/b": "yaml_import"}


def test_a_node_or_model_without_a_configuration_stores_nothing(store):
    store.config = None
    assert "unknown" in asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 1}))

    store.config = {"revision": 1, "entries": [_entry("org/b")]}
    assert "no configuration" in asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 1}))
    assert store.saved == []


def test_a_conflict_is_tried_once_more_with_the_newer_revision(store):
    store.config = {"revision": 3, "entries": [_entry("org/a")]}
    store.results = [_response(409, "Configuration changed meanwhile (revision 4); reload it")]

    reason = asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 1}))

    assert reason is None
    assert len(store.saved) == 2


def test_a_second_conflict_and_a_refusal_are_reported_not_raised(store):
    store.config = {"revision": 3, "entries": [_entry("org/a")]}
    store.results = [_response(409, "again"), _response(409, "once more")]
    assert asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 1})) == "once more"

    store.saved.clear()
    store.results = [_response(400, "org/a: max_num_seqs: nope")]
    assert asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 1})) == "org/a: max_num_seqs: nope"
    assert len(store.saved) == 1

    store.results = [SimpleNamespace(status_code=500, body=b"not json")]
    assert asyncio.run(mod.record_adoption(7, "org/a", {"max_num_seqs": 1})) == "refused"


def test_the_scheduler_stores_the_adoption_and_survives_every_failure(monkeypatch, caplog):
    calls = []

    async def record(provider_id, model, parameters):
        calls.append((provider_id, model, parameters))
        return None

    monkeypatch.setattr(scheduler_mod, "record_adoption", record)
    asyncio.run(NightlyScheduler.store_adoption({"provider_id": 7, "model": "org/a"}, {"parameters": {"max_num_seqs": 2}}))
    assert calls == [(7, "org/a", {"max_num_seqs": 2})]

    asyncio.run(NightlyScheduler.store_adoption({"provider_id": 7}, {"parameters": {}}))
    assert len(calls) == 1

    async def broken(*_args):
        raise RuntimeError("database down")

    monkeypatch.setattr(scheduler_mod, "record_adoption", broken)
    asyncio.run(NightlyScheduler.store_adoption({"provider_id": 7, "model": "org/a"}, {"parameters": {}}))
    assert "database down" in caplog.text

    async def refused(*_args):
        return "worker refused"

    monkeypatch.setattr(scheduler_mod, "record_adoption", refused)
    asyncio.run(NightlyScheduler.store_adoption({"provider_id": 7, "model": "org/a"}, {"parameters": {}}))
    assert "worker refused" in caplog.text
