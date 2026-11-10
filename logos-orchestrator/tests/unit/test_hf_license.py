import httpx
import pytest
from huggingface_hub.errors import GatedRepoError, HFValidationError, RepositoryNotFoundError

from logos import hf_license


@pytest.fixture
def hub(monkeypatch):
    """Serve the Hub API requests from ``hub.handler``; record each request."""

    class _Hub:
        requests: list[httpx.Request] = []

        @staticmethod
        def handler(request):
            return httpx.Response(200, json={})

    def _client():
        return httpx.AsyncClient(
            timeout=hf_license._TIMEOUT_S,
            transport=httpx.MockTransport(lambda request: _Hub.requests.append(request) or _Hub.handler(request)),
        )

    monkeypatch.setattr(hf_license, "_new_client", _client)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    return _Hub


def _hub_with(hub, model_info, tree_by_ref, files):
    def handler(request):
        path = str(request.url.path)
        if path.endswith("/api/models/org/model"):
            return httpx.Response(200, json=model_info)
        match = path.split("/api/models/org/model/")
        if len(match) > 1 and match[1].startswith("tree/"):
            ref = match[1].removeprefix("tree/")
            if ref in tree_by_ref:
                return httpx.Response(200, json=tree_by_ref[ref])
            return httpx.Response(404, json={"error": "Not Found"})
        # The license file comes from the canonical resolve URL — without
        # the /api prefix — so it is matched on its own path.
        match = path.split("/org/model/resolve/")
        if len(match) > 1:
            file_path = match[1].split("/", 1)[1]
            if file_path in files:
                return httpx.Response(200, text=files[file_path])
            return httpx.Response(404, json={"error": "Not Found"})
        return httpx.Response(404, json={"error": "Not Found"})

    hub.handler = staticmethod(handler)


async def test_license_file_and_card_license(hub):
    _hub_with(
        hub,
        {"cardData": {"license": "apache-2.0"}},
        {
            "main": [
                {"type": "file", "path": "LICENSE.md"},
                {"type": "file", "path": "model.safetensors"},
            ]
        },
        {"LICENSE.md": "Apache License 2.0 ..."},
    )

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_id == "apache-2.0"
    assert result.license_file == "LICENSE.md"
    assert result.license_text == "Apache License 2.0 ..."
    paths = [str(r.url.path) for r in hub.requests]
    assert paths == [
        "/api/models/org/model",
        "/api/models/org/model/tree/main",
        "/org/model/resolve/main/LICENSE.md",
    ]


async def test_lfs_license_file_follows_the_cdn_redirect(hub):
    def handler(request):
        path = str(request.url.path)
        if path.endswith("/api/models/org/model"):
            return httpx.Response(200, json={"cardData": {"license": "apache-2.0"}})
        if path == "/api/models/org/model/tree/main":
            return httpx.Response(200, json=[{"type": "file", "path": "LICENSE"}])
        if path == "/org/model/resolve/main/LICENSE":
            # LFS files 302 to CDN storage; the file text lives at the target.
            return httpx.Response(302, headers={"location": "https://cdn.example/lic"}, text="")
        if path == "/lic":
            return httpx.Response(200, text="Apache License 2.0 ...")
        return httpx.Response(404, json={"error": "Not Found"})

    hub.handler = staticmethod(handler)

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_text == "Apache License 2.0 ..."


async def test_provider_specific_license_file_names_are_found(hub):
    _hub_with(
        hub,
        {"cardData": {"license": "llama3_1"}},
        {
            "main": [
                {"type": "file", "path": "model.safetensors"},
                {"type": "file", "path": "community-license-agreement.md"},
            ]
        },
        {"community-license-agreement.md": "Llama 3.1 Community License ..."},
    )

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_file == "community-license-agreement.md"
    assert result.license_text.startswith("Llama 3.1")


async def test_license_id_wins_when_several_files_match(hub):
    _hub_with(
        hub,
        {"cardData": {"license": "mit"}},
        {
            "main": [
                {"type": "file", "path": "LICENSE.txt"},
                {"type": "file", "path": "COPYING"},
                {"type": "file", "path": "LICENCES/extra.md"},
            ]
        },
        {"LICENSE.txt": "MIT ..."},
    )

    result = await hf_license.fetch_hf_license("org/model")

    assert result.license_file == "LICENSE.txt"
    assert result.license_text == "MIT ..."


async def test_no_license_file_keeps_the_card_license(hub):
    _hub_with(hub, {"cardData": {"license": "mit"}}, {"main": [{"type": "file", "path": "model.safetensors"}]}, {})

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_id == "mit"
    assert result.license_file is None
    assert result.license_text is None


async def test_no_license_information_is_no_error(hub):
    _hub_with(hub, {"cardData": None}, {"main": [{"type": "file", "path": "model.safetensors"}]}, {})

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_id is None
    assert result.license_file is None
    assert result.license_text is None


async def test_multiple_declared_licenses_become_an_or_expression(hub):
    _hub_with(hub, {"cardData": {"license": ["mit", "apache-2.0"]}}, {"main": []}, {})

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_id == "mit OR apache-2.0"


async def test_master_is_tried_when_main_is_missing(hub):
    _hub_with(
        hub,
        {"cardData": {"license": "bllm"}},
        {"master": [{"type": "file", "path": "LICENSE"}]},
        {"LICENSE": "BigScience License ..."},
    )

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is None
    assert result.license_file == "LICENSE"
    assert result.license_text.startswith("BigScience")
    assert "/api/models/org/model/tree/main" in [str(r.url.path) for r in hub.requests]


def _raise_for(error):
    def _raise(response, endpoint_name=None):
        if response.status_code >= 400:
            raise error

    return _raise


@pytest.mark.parametrize(
    ("status_code", "error", "expected"),
    [
        (403, GatedRepoError("gated"), "requires an authorized HF_TOKEN"),
        (401, RepositoryNotFoundError("missing"), "does not exist or is not visible"),
    ],
)
async def test_unreadable_repository_is_an_error_not_a_verdict(hub, monkeypatch, status_code, error, expected):
    hub.handler = staticmethod(lambda request: httpx.Response(status_code))
    monkeypatch.setattr("huggingface_hub.utils.hf_raise_for_status", _raise_for(error))

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is not None
    assert expected in result.error
    assert result.license_id is None
    assert result.license_text is None


async def test_malformed_repo_id_is_rejected_before_asking_the_hub(hub, monkeypatch):
    def _invalid(repo_id):
        raise HFValidationError(f"bad id {repo_id}")

    monkeypatch.setattr("huggingface_hub.utils.validate_repo_id", _invalid)

    result = await hf_license.fetch_hf_license("bad//repo id")

    assert result.error is not None
    assert hub.requests == []


async def test_hub_failure_is_an_error_to_retry(hub):
    def _refuse(request):
        raise httpx.ConnectError("proxy.internal.example:3128 refused", request=request)

    hub.handler = staticmethod(_refuse)

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is not None
    assert "proxy.internal" not in result.error


async def test_slow_hub_times_out_as_an_error(hub):
    def _slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    hub.handler = staticmethod(_slow)

    result = await hf_license.fetch_hf_license("org/model")

    assert result.error is not None
    assert "did not answer" in result.error


async def test_central_hf_token_is_used(hub, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", " hf_secret ")
    _hub_with(hub, {"cardData": None}, {"main": []}, {})

    await hf_license.fetch_hf_license("org/model")

    assert all(r.headers.get("authorization") == "Bearer hf_secret" for r in hub.requests)


async def test_no_token_means_no_authorization_header(hub):
    _hub_with(hub, {"cardData": None}, {"main": []}, {})

    await hf_license.fetch_hf_license("org/model")

    assert all("authorization" not in r.headers for r in hub.requests)
