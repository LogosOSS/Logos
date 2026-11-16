"""The worker's models come from Logos; the plans measured from them must not drift."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
import yaml

from logos_worker_node.calibration import plans_from_config
from logos_worker_node.config import _wire_kv_budget
from logos_worker_node.model_profiles import (
    _FLOAT_OVERRIDES,
    _INT_OVERRIDES,
    PROFILE_OVERRIDE_KEYS,
    ModelProfileRegistry,
)
from logos_worker_node.models import AppConfig
from logos_worker_node.node_config import (
    apply_node_config,
    entries_from_yaml,
    entries_from_yaml_file,
    normalize_node_config,
    plans_from_app_config,
    plans_from_entries,
    profile_overrides_of,
    validation_errors,
)
from logos_worker_node.profile_fingerprint import plan_hash

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ["config.yml", "config.example.yml", "config.example.mlx.yml"]

YAML = """
logos:
  enabled: true
  capabilities_models:
    - org/plain
    - model: org/big
      tensor_parallel_size: 2
      kv_cache_memory_bytes: 16G
      base_residency_mb: 5000
  capabilities_overrides:
    org/big:
      min_context_fraction: 0.5
engines:
  vllm:
    global_extra_args: ["--seed", "1"]
    model_overrides:
      org/big:
        kv_cache_memory_bytes: 8G
        quantization: awq
        extra_args: ["--max-num-batched-tokens", "4096"]
        _note: ignored
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(text)
    return path


def _central_cfg(path: Path) -> AppConfig:
    """What a worker holds after it fetched its models from Logos."""
    cfg = AppConfig(**yaml.safe_load(path.read_text()))
    _wire_kv_budget(cfg)
    # Logos stores JSON; the entries come back through it.
    stored = json.loads(json.dumps(entries_from_yaml_file(path)))
    apply_node_config(cfg, stored, revision=3)
    return cfg


def test_yaml_entries_keep_both_places_apart(tmp_path):
    entries = {e["model_name"]: e for e in entries_from_yaml(yaml.safe_load(_write(tmp_path, YAML).read_text()))}

    assert list(entries) == ["org/plain", "org/big"]
    assert entries["org/plain"] == {"model_name": "org/plain", "capability": {}, "overrides": {}}
    # Keys of an explicit capabilities_overrides join the inline ones.
    assert entries["org/big"]["capability"] == {
        "tensor_parallel_size": 2,
        "kv_cache_memory_bytes": "16G",
        "base_residency_mb": 5000,
        "min_context_fraction": 0.5,
    }
    assert entries["org/big"]["overrides"]["kv_cache_memory_bytes"] == "8G"


def test_yaml_entries_skip_blank_repeated_and_malformed_models():
    raw = {"logos": {"capabilities_models": ["a", " a ", "", {"tensor_parallel_size": 2}, 7, {"model": "b"}]}}

    assert [e["model_name"] for e in entries_from_yaml(raw)] == ["a", "b"]


def test_a_config_without_logos_has_no_entries():
    assert entries_from_yaml({}) == []
    assert entries_from_yaml({"logos": {"capabilities_models": None}}) == []


def test_plans_put_capability_before_overrides_and_skip_bookkeeping(tmp_path):
    cfg = _central_cfg(_write(tmp_path, YAML))

    plans = {p["model"]: p for p in plans_from_app_config(cfg)}

    big = plans["org/big"]
    assert big["kv_cache_memory_bytes"] == "16G"  # capability wins in a plan
    assert big["quantization"] == "awq"
    assert "_note" not in big
    assert big["extra_args"] == ["--seed", "1", "--max-num-batched-tokens", "4096"]
    assert plans["org/plain"]["extra_args"] == ["--seed", "1"]


def test_plans_without_entries_are_empty():
    assert plans_from_app_config(AppConfig()) == []
    assert plans_from_entries([], ["--seed"]) == []


def test_apply_replaces_the_models_in_order(tmp_path):
    cfg = AppConfig(**yaml.safe_load(_write(tmp_path, YAML).read_text()))
    entries = [
        {"model_name": "org/new", "capability": {"base_residency_mb": 900}, "overrides": {"dtype": "half"}},
        {"model_name": "org/big", "capability": {}, "overrides": {}},
    ]

    apply_node_config(cfg, entries, revision=7)

    assert cfg.logos.capabilities_models == ["org/new", "org/big"]
    assert cfg.logos.configured_models == ["org/new", "org/big"]
    assert cfg.logos.capabilities_overrides == {"org/new": {"base_residency_mb": 900}}
    assert cfg.engines.vllm.model_overrides["org/new"] == {"dtype": "half"}
    # The YAML override of a model Logos lists without any is dropped.
    assert "org/big" not in cfg.engines.vllm.model_overrides
    assert cfg.logos.node_config_revision == 7


def test_apply_wires_the_kv_budget_without_touching_the_stored_entries():
    cfg = AppConfig()
    entries = [{"model_name": "org/m", "capability": {"kv_cache_memory_bytes": "4G"}, "overrides": {}}]

    apply_node_config(cfg, entries, revision=1)

    assert cfg.engines.vllm.model_overrides["org/m"]["kv_cache_memory_bytes"] == "4G"
    assert cfg.logos.capabilities_overrides["org/m"]["kv_budget_mb"] == 4096.0
    assert cfg.logos.node_entries == entries
    assert "kv_budget_mb" not in cfg.logos.node_entries[0]["capability"]
    assert entries[0]["overrides"] == {}


def test_applying_a_newer_revision_drops_what_the_older_one_wired():
    cfg = AppConfig()
    apply_node_config(cfg, [{"model_name": "m", "capability": {"kv_cache_memory_bytes": "4G"}, "overrides": {}}], 1)

    apply_node_config(cfg, [{"model_name": "m", "capability": {}, "overrides": {}}], 2)

    assert "m" not in cfg.engines.vllm.model_overrides
    assert cfg.logos.capabilities_overrides == {}


def test_apply_keeps_overrides_of_models_logos_does_not_list():
    cfg = AppConfig(**{"engines": {"vllm": {"model_overrides": {"org/static": {"dtype": "half"}}}}})

    apply_node_config(cfg, [{"model_name": "org/m", "capability": {}, "overrides": {}}], 1)

    assert cfg.engines.vllm.model_overrides == {"org/static": {"dtype": "half"}}


@pytest.mark.parametrize("name", EXAMPLES)
def test_central_plans_hash_like_the_yaml_plans(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout")
    cfg = _central_cfg(path)

    central = {p["model"]: plan_hash(p) for p in plans_from_app_config(cfg)}
    from_yaml = {p["model"]: plan_hash(p) for p in plans_from_config(path)}

    assert central == from_yaml
    assert central  # the examples configure models


def test_logos_answer_is_normalized():
    body = {"revision": "3", "yaml_imported": 1, "entries": [{"model_name": " org/m ", "capability": None}]}

    assert normalize_node_config(body) == {
        "revision": 3,
        "yaml_imported": True,
        "entries": [{"model_name": "org/m", "capability": {}, "overrides": {}}],
    }


@pytest.mark.parametrize("body", [None, {}, {"entries": "x"}, {"entries": [{"capability": {}}]}, {"entries": [7]}])
def test_an_unusable_logos_answer_is_refused(body):
    with pytest.raises(RuntimeError):
        normalize_node_config(body)


def _entry(overrides=None, capability=None):
    return {"model_name": "org/a", "capability": capability or {}, "overrides": overrides or {}}


def test_a_misspelled_override_is_refused_with_a_suggestion():
    errors = validation_errors([_entry({"max_modell_len": 8192})])
    assert errors == ["org/a: max_modell_len: unknown setting (did you mean max_model_len?)"]


def test_an_override_without_a_close_match_is_refused_without_a_hint():
    assert validation_errors([_entry({"zzzzzz": 1})]) == ["org/a: zzzzzz: unknown setting"]


def test_engine_and_profile_override_keys_are_accepted():
    overrides = {"max_model_len": 8192, "quantization": "awq", "max_context_length": 4096, "host_ram_mb": 100}
    assert validation_errors([_entry(overrides)]) == []


def test_a_wrong_value_is_still_refused():
    assert validation_errors([_entry({"max_model_len": "lots"})])


def test_profile_override_keys_match_what_the_registry_reads():
    source = inspect.getsource(ModelProfileRegistry._apply_manual_overrides)
    assert set(re.findall(r'"(\w+)" in overrides', source)) == PROFILE_OVERRIDE_KEYS


def test_the_model_kind_of_a_plan_is_accepted():
    assert validation_errors([_entry({"model_kind": "embedding"})]) == []
    assert validation_errors([_entry({"model_kind": 3})]) == ["org/a: model_kind: must be a string"]


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"host_ram_mb": "lots"}, "org/a: host_ram_mb: must be a number"),
        ({"base_residency_mb": True}, "org/a: base_residency_mb: must be a number"),
        ({"min_context_fraction": float("nan")}, "org/a: min_context_fraction: must be a number"),
        ({"max_context_length": "big"}, "org/a: max_context_length: must be an integer"),
        ({"metal_capacity_floor_mb": "x"}, "org/a: metal_capacity_floor_mb: must be a number or null"),
        ({"kv_cache_to_max_model_len_pairs": "x"}, "org/a: kv_cache_to_max_model_len_pairs: must be a list"),
    ],
)
def test_a_profile_override_that_cannot_be_applied_is_refused(overrides, message):
    assert validation_errors([_entry(overrides)]) == [message]


def test_profile_override_values_the_registry_converts_are_accepted():
    overrides = {
        "host_ram_mb": "1024",
        "max_context_length": 4096,
        "min_context_fraction": 0.5,
        "metal_capacity_floor_mb": None,
        "kv_cache_to_max_model_len_pairs": [{"kv_mb": 1, "max_model_len": 2}],
    }
    assert validation_errors([_entry(overrides)]) == []


def test_every_profile_override_key_has_a_value_check():
    checked = {*_FLOAT_OVERRIDES, *_INT_OVERRIDES, "metal_capacity_floor_mb", "kv_cache_to_max_model_len_pairs"}
    assert PROFILE_OVERRIDE_KEYS - checked == {"engine"}


def test_profile_overrides_of_the_stored_models_cover_capability_and_routed_keys():
    cfg = AppConfig()
    apply_node_config(
        cfg,
        [
            {
                "model_name": "org/a",
                "capability": {"kv_cache_memory_bytes": "2G", "base_residency_mb": 900},
                "overrides": {"quantization": "awq", "host_ram_mb": 10},
            },
            {"model_name": "org/b", "capability": {}, "overrides": {"quantization": "awq"}},
        ],
        1,
    )
    found = profile_overrides_of(cfg)
    assert set(found) == {"org/a"}
    assert found["org/a"]["base_residency_mb"] == 900 and found["org/a"]["host_ram_mb"] == 10
    assert found["org/a"]["kv_budget_mb"] == 2048.0 and "quantization" not in found["org/a"]
