from __future__ import annotations

import builtins
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from logos_worker_node.vllm_compat import FALLBACK_GMU


@pytest.mark.parametrize("options", [[], ["--dtype", "bfloat16", "--quantization", "awq", "--trust-remote-code"]])
def test_standalone_converter_preserves_gpu_budget(monkeypatch, tmp_path: Path, options: list[str]) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    output = tmp_path / "sharded"
    save = Mock()
    engine_args = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    from_engine_args = Mock(
        return_value=SimpleNamespace(
            llm_engine=SimpleNamespace(
                engine_core=SimpleNamespace(
                    save_sharded_state=save,
                )
            )
        )
    )
    vllm = ModuleType("vllm")
    vllm.EngineArgs = engine_args
    vllm.LLM = SimpleNamespace(from_engine_args=from_engine_args)
    loader = ModuleType("vllm.model_executor.model_loader")
    loader.ShardedStateLoader = SimpleNamespace(DEFAULT_PATTERN="model-rank-{rank}-part-{part}.safetensors")
    monkeypatch.setitem(sys.modules, "vllm", vllm)
    monkeypatch.setitem(sys.modules, "vllm.model_executor", ModuleType("vllm.model_executor"))
    monkeypatch.setitem(sys.modules, "vllm.model_executor.model_loader", loader)
    entrypoint = Path(__file__).resolve().parents[1] / "logos_worker_node" / "_sharded_convert.py"
    monkeypatch.setattr(
        sys,
        "argv",
        [str(entrypoint), "--model", str(model), "--tensor-parallel-size", "2", "--output", str(output), *options],
    )

    # The serving interpreter may have vLLM but no installed worker package.
    original_import = builtins.__import__

    def import_without_worker(name, *args, **kwargs):
        if name == "logos_worker_node" or name.startswith("logos_worker_node."):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_worker)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(entrypoint), run_name="__main__")
    assert exc.value.code == 0
    engine_args.assert_called_once_with(
        model=str(model),
        tensor_parallel_size=2,
        dtype="bfloat16" if options else "auto",
        quantization="awq" if options else None,
        trust_remote_code=bool(options),
        gpu_memory_utilization=FALLBACK_GMU,
    )
    from_engine_args.assert_called_once()
    assert from_engine_args.call_args.args[0].gpu_memory_utilization == FALLBACK_GMU
    save.assert_called_once_with(
        path=str(output), pattern=loader.ShardedStateLoader.DEFAULT_PATTERN, max_size=5 * 1024**3
    )
    assert (output / "config.json").read_text() == "{}"
