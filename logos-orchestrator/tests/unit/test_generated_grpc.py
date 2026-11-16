"""Check generated bindings with real libraries outside the unit-test dependency stubs."""

import subprocess
import sys
from pathlib import Path


def test_generated_grpc_bindings_match_the_installed_protobuf_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from grpclocal import model_pb2, model_pb2_grpc; "
            "request = model_pb2.GenerateRequest(path='/chat/completions', metadata={'test': 'value'}, payload='{}'); "
            "decoded = model_pb2.GenerateRequest.FromString(request.SerializeToString()); "
            "assert decoded == request; "
            "assert model_pb2_grpc.LogosStub",
        ],
        cwd=Path(__file__).resolve().parents[2] / "src",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
