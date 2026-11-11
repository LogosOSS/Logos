"""Deployment credentials are opt-in for each host role."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/write-deployment-secrets.py"))
render_secrets = SCRIPT["render_secrets"]


def _environment():
    names = (*SCRIPT["CORE_SECRETS"], *SCRIPT["WORKER_SECRETS"], "NEW_REPOSITORY_SECRET", "VM_SSH_PRIVATE_KEY")
    return {"DEPLOY_SECRET_" + name: "test-" + name for name in names}


@pytest.mark.parametrize("target,names", [("core", SCRIPT["CORE_SECRETS"]), ("worker", SCRIPT["WORKER_SECRETS"])])
def test_only_target_credentials_are_exported(target, names):
    output = render_secrets(target, _environment())
    assert {line.split("=", 1)[0] for line in output.splitlines()} == set(names)
    assert "NEW_REPOSITORY_SECRET" not in output
    assert "VM_SSH_PRIVATE_KEY" not in output


@pytest.mark.parametrize("name", ["LOGOS_DB_PASSWORD", "LOGOS_REDIS_PASSWORD"])
def test_core_passwords_are_required(name):
    env = _environment()
    env["DEPLOY_SECRET_" + name] = ""
    with pytest.raises(ValueError, match=name):
        render_secrets("core", env)


def test_worker_key_is_required():
    with pytest.raises(ValueError, match="LOGOS_API_KEY"):
        render_secrets("worker", {})


def test_optional_credentials_are_omitted():
    assert render_secrets("worker", {"DEPLOY_SECRET_LOGOS_API_KEY": "key"}) == 'LOGOS_API_KEY="key"\n'


@pytest.mark.parametrize("value", ["test\ninjected=value", "test\rvalue", "test\0value"])
def test_secret_cannot_add_dotenv_lines(value):
    env = _environment()
    env["DEPLOY_SECRET_LOGOS_DB_PASSWORD"] = value
    with pytest.raises(ValueError, match="single line"):
        render_secrets("core", env)


def test_dollars_quotes_and_backslashes_are_preserved():
    value = "x$UNSET#y'\"z@:/\\"
    output = render_secrets("worker", {"DEPLOY_SECRET_LOGOS_API_KEY": value})
    quoted = output.strip().split("=", 1)[1]
    assert json.loads(quoted).replace("$$", "$") == value


def test_unknown_target_is_rejected():
    with pytest.raises(ValueError, match="Unknown"):
        render_secrets("other", _environment())
