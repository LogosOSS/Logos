"""The Responses image tool: the key's access to its image model, and Azure's deployment header."""

import pytest

from logos.image_generation_tool import (
    AZURE_IMAGE_DEPLOYMENT_HEADER,
    azure_image_deployment_headers,
    image_tool_access_error,
    requested_image_models,
)
from logos.pipeline import context_resolver
from logos.pipeline.context_resolver import ContextResolver, ExecutionContext

DEPLOYMENTS = [
    {"model_id": 1, "provider_id": 8, "model_name": "gpt-5.6-luna", "aliases": None},
    {"model_id": 2, "provider_id": 8, "model_name": "gpt-image-1.5", "aliases": "image-hq"},
]


def _payload(*tools):
    return {"model": "gpt-5.6-luna", "input": "draw", "tools": list(tools)}


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_provider_model_deployments(self, provider_id):
        return self.rows


@pytest.fixture
def azure_rows(monkeypatch):
    rows = [
        {
            "model_id": 2,
            "model_name": "gpt-image-1.5",
            "endpoint": "https://res.openai.azure.com/openai/deployments/img-15/images/generations?api-version=x",
            "aliases": "image-hq",
        }
    ]
    # Patched through the functions' own globals: the test session may hold
    # more than one copy of the module, and each caller resolves DBManager in
    # the copy it imported from.
    for function in (azure_image_deployment_headers, context_resolver.azure_image_deployment_headers):
        monkeypatch.setitem(function.__globals__, "DBManager", lambda: _FakeDB(rows))
    return rows


def test_the_tool_model_defaults_to_the_openai_default():
    assert requested_image_models(_payload({"type": "image_generation"})) == ["gpt-image-1"]


def test_other_tools_and_bodies_without_tools_name_no_image_model():
    assert requested_image_models(_payload({"type": "web_search"})) == []
    assert requested_image_models({"model": "x"}) == []
    assert requested_image_models(None) == []


def test_a_permitted_image_model_passes_by_name_or_alias():
    assert (
        image_tool_access_error(_payload({"type": "image_generation", "model": "gpt-image-1.5"}), DEPLOYMENTS) is None
    )
    assert image_tool_access_error(_payload({"type": "image_generation", "model": "IMAGE-HQ"}), DEPLOYMENTS) is None


def test_an_image_model_the_key_may_not_use_is_refused():
    error = image_tool_access_error(_payload({"type": "image_generation", "model": "gpt-image-2"}), DEPLOYMENTS)
    assert error is not None and "gpt-image-2" in error


def test_two_different_image_models_in_one_request_are_refused():
    error = image_tool_access_error(
        _payload(
            {"type": "image_generation", "model": "gpt-image-1.5"},
            {"type": "image_generation", "model": "image-hq-2"},
        ),
        DEPLOYMENTS,
    )
    assert error is not None and "one image model" in error


def test_the_header_names_the_image_deployment_on_the_provider(azure_rows):
    headers = azure_image_deployment_headers(8, _payload({"type": "image_generation", "model": "gpt-image-1.5"}))
    assert headers == {AZURE_IMAGE_DEPLOYMENT_HEADER: "img-15"}


def test_an_image_model_alias_resolves_to_the_canonical_deployment(azure_rows):
    headers = azure_image_deployment_headers(8, _payload({"type": "image_generation", "model": "IMAGE-HQ"}))
    assert headers == {AZURE_IMAGE_DEPLOYMENT_HEADER: "img-15"}


def test_no_header_without_the_tool_or_without_the_deployment(azure_rows):
    assert azure_image_deployment_headers(8, _payload({"type": "web_search"})) == {}
    assert azure_image_deployment_headers(8, _payload({"type": "image_generation", "model": "gpt-image-2"})) == {}


def _context(**overrides):
    base = dict(
        model_id=1,
        provider_id=8,
        provider_name="azure",
        provider_type="cloud",
        forward_url="https://res.openai.azure.com/openai/responses?api-version=x",
        auth_header="api-key",
        auth_value="az-key",
        model_name="gpt-5.6-luna",
        azure_body_deployment="gpt-5.6-luna",
        cloud_provider_type="azure",
    )
    base.update(overrides)
    return ExecutionContext(**base)


def test_an_azure_forward_carries_the_image_deployment_header(azure_rows):
    headers, payload = ContextResolver.prepare_headers_and_payload(
        _context(), _payload({"type": "image_generation", "model": "gpt-image-1.5"})
    )
    assert headers[AZURE_IMAGE_DEPLOYMENT_HEADER] == "img-15"
    assert payload["tools"] == [{"type": "image_generation", "model": "gpt-image-1.5"}]


def test_a_non_azure_forward_gets_no_image_header(azure_rows):
    headers, _ = ContextResolver.prepare_headers_and_payload(
        _context(cloud_provider_type="openai", azure_body_deployment=None),
        _payload({"type": "image_generation", "model": "gpt-image-1.5"}),
    )
    assert AZURE_IMAGE_DEPLOYMENT_HEADER not in headers
