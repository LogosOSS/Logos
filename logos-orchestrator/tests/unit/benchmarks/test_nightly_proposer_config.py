from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from logos.benchmarks import nightly_proposer_config as config


@pytest.fixture
def db(monkeypatch):
    database = Mock()
    monkeypatch.setattr(config, "DBManager", lambda: nullcontext(database))
    return database


def test_saved_model_takes_precedence_over_the_deployment_default(db, monkeypatch):
    result = db.session.execute.return_value
    result.one_or_none.return_value = None
    monkeypatch.delenv("LOGOS_NIGHTLY_PROD_DEFAULT_MODEL", raising=False)
    assert config.selection() == config.ProposerModel("local", config.DEFAULT_LOCAL_MODEL)

    monkeypatch.setenv("LOGOS_NIGHTLY_PROD_DEFAULT_MODEL", "prod/default")
    assert config.selection() == config.ProposerModel("logos_prod", "prod/default")

    result.one_or_none.return_value = SimpleNamespace(provider="local", model="local/selected", revision=3)
    assert config.selection() == config.ProposerModel("local", "local/selected", 3)


@pytest.mark.parametrize(("expected", "revision"), [(None, 1), (3, 4), (3, None)])
def test_saving_a_selection_checks_the_previous_revision(db, expected, revision):
    db.session.execute.return_value.scalar_one_or_none.return_value = revision

    saved = config.save_selection("logos_prod", "prod/model", expected)

    assert db.session.execute.call_args.args[1] == {
        "provider": "logos_prod",
        "model": "prod/model",
        "revision": expected,
    }
    if revision is None:
        assert saved is None
    else:
        assert saved == config.ProposerModel("logos_prod", "prod/model", revision)
    db.session.commit.assert_called_once()


async def test_local_catalog_deduplicates_models_and_excludes_cloud_deployments(db):
    db.get_agent_api_key_id.return_value = 4
    db.get_deployments_for_api_key.return_value = [
        {"type": "logosnode", "model_name": "local/z"},
        {"type": "logosnode", "model_name": "local/a"},
        {"type": "logosnode", "model_name": "local/z"},
        {"type": "cloud", "model_name": "cloud/model"},
    ]

    assert await config.catalog("local") == ["local/a", "local/z"]
    db.get_deployments_for_api_key.assert_called_once_with(4)

    db.get_agent_api_key_id.return_value = None
    assert config.local_models() == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://prod.test/v1",
        "https://user:password@prod.test/v1",
        "https://prod.test/v1?a=1",
        "https://prod.test:999x/v1",
    ],
)
async def test_prod_catalog_requires_an_https_gateway_and_configured_key(monkeypatch, url):
    monkeypatch.setenv("LOGOS_NIGHTLY_PROD_BASE_URL", url)
    monkeypatch.setenv("LOGOS_NIGHTLY_PROD_API_KEY", "fixture-key")

    with pytest.raises(ValueError, match="not configured"):
        await config.catalog("logos_prod")

    monkeypatch.setenv("LOGOS_NIGHTLY_PROD_BASE_URL", "https://prod.test/v1")
    monkeypatch.delenv("LOGOS_NIGHTLY_PROD_API_KEY")
    assert config.LogosProdGateway.from_env() is None


@pytest.fixture
def prod_fixture(monkeypatch):
    monkeypatch.setenv("LOGOS_NIGHTLY_PROD_BASE_URL", "https://prod.test/v1/")
    monkeypatch.setenv("LOGOS_NIGHTLY_PROD_API_KEY", "fixture-key")
    client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs)
        )

    return install


async def test_prod_catalog_uses_the_scoped_key_and_filters_invalid_models(prod_fixture):
    def serve(request):
        assert str(request.url) == "https://prod.test/v1/models"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer fixture-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model/z"},
                    {"id": "model/a"},
                    {"id": "model/z"},
                    {"id": ""},
                    {"id": 7},
                    {"id": "x" * 256},
                    {},
                ]
            },
        )

    prod_fixture(serve)
    assert await config.catalog("logos_prod") == ["model/a", "model/z"]


@pytest.mark.parametrize("body", [[], {"data": None}])
async def test_invalid_prod_catalog_is_rejected(prod_fixture, body):
    prod_fixture(lambda _: httpx.Response(200, json=body))

    with pytest.raises(ValueError, match="Unexpected Logos PROD"):
        await config.catalog("logos_prod")


async def test_prod_catalog_does_not_follow_redirects(prod_fixture):
    request = Mock(return_value=httpx.Response(302, headers={"Location": "https://other.test/v1/models"}))
    prod_fixture(request)

    with pytest.raises(httpx.HTTPStatusError):
        await config.catalog("logos_prod")

    request.assert_called_once()
