"""The internal multipart marker cannot come from a client, and the forwarded form names the authorized model."""

import asyncio

import pytest
from fastapi import HTTPException

from logos import batch_local
from logos.pipeline.context_resolver import ContextResolver, ExecutionContext
from logos.pipeline.executor import Executor
from logos.request_content import MULTIPART_PAYLOAD_KEY, reject_client_multipart_marker


def _smuggled(model_in_form: str) -> dict:
    return {
        MULTIPART_PAYLOAD_KEY: {
            "fields": [["model", model_in_form]],
            "files": [
                {
                    "field_name": "image",
                    "filename": "a.png",
                    "content_type": "image/png",
                    "content_base64": "AA==",
                    "size": 1,
                }
            ],
        }
    }


def test_client_body_with_marker_is_rejected():
    with pytest.raises(HTTPException) as exc:
        reject_client_multipart_marker(_smuggled("forbidden-model"))
    assert exc.value.status_code == 400


def test_client_body_without_marker_passes():
    reject_client_multipart_marker({"model": "allowed-model", "messages": []})


def test_local_batch_line_with_marker_fails_without_running():
    line = {"custom_id": "c1", "url": "/v1/images/edits", "body": _smuggled("forbidden-model")}
    row, _ = asyncio.run(batch_local._run_line(line, object(), {}))
    assert row["response"]["status_code"] == 400


def test_forwarded_form_names_the_authorized_model():
    context = ExecutionContext(
        model_id=1,
        provider_id=1,
        provider_name="OpenAI",
        provider_type="cloud",
        forward_url="https://api.openai.com/v1/images/edits",
        auth_header="Authorization",
        auth_value="Bearer secret",
        model_name="allowed-model",
    )
    _, payload = ContextResolver.prepare_headers_and_payload(context, _smuggled("forbidden-model"))
    sent = Executor._request_kwargs(payload)["files"]
    models = [part[1][1] for part in sent if part[0] == "model"]
    assert models == ["allowed-model"]
