"""Opt-in test of the shipped nginx image, never a running Logos deployment.

RUN_AGENT_GATEWAY_TESTS=1 python -m pytest agent-gateway/tests
"""

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_AGENT_GATEWAY_TESTS") != "1", reason="Opt-in disposable Docker test")


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


SECRET = "test-internal-secret"
# Every character with a meaning in an nginx configuration or a shell.
HOSTILE_SECRET = 'a")(b!c]#d\\"(e@\\f[g.h!i^j[k\'l`m$n${o};p{q}r s'


def authorizations(upstream):
    """The Authorization headers the credential issuer received, in order."""
    return docker("exec", upstream, "cat", "/tmp/authorization.log").splitlines()


@pytest.fixture
def gateway(tmp_path, request):
    config_override = getattr(request, "param", SECRET)
    secret, mode = config_override if isinstance(config_override, tuple) else (config_override, "local")
    root = Path(__file__).resolve().parents[2]
    identity = "agent-gateway-test-" + uuid.uuid4().hex[:12]
    upstream, proxy = identity + "-upstream", identity + "-proxy"
    config = tmp_path / "upstream.conf"
    config.write_text("""log_format authorization escape=none '$http_authorization';
    server {
        listen 8080;
        location = /internal/agent_credentials {
            access_log /tmp/authorization.log authorization;
            if (-f /tmp/revoked) { return 403; }
            add_header X-Logos-Agent-Token "test-short-lived-token" always;
            return 204;
        }
        location / { return 503; }
    }
    server {
        listen 8081;
        location /v1/ {
            default_type application/json;
            add_header X-Logos-Agent-Relay-Secret "not visible to callers";
            add_header X-Logos-Agent-Inference "not visible to callers";
            add_header Authorization "not visible to callers";
            return 200 '{"path":"$uri","authorization":"$http_authorization",
                         "secret":"$http_x_logos_agent_relay_secret", "tag":"$http_x_logos_client_tag",
                         "mode":"$http_x_logos_agent_inference",
                         "alternative":"$http_logos_key", "anthropic":"$http_x_api_key",
                         "anthropic_version":"$http_anthropic_version","anthropic_beta":"$http_anthropic_beta"}';
        }
    }""")
    # Use the published image's root context and allowlist, not an unfiltered hand-made context.
    docker("build", "-q", "-t", identity, "-f", str(root / "agent-gateway/Dockerfile"), str(root))
    docker("network", "create", identity)
    try:
        docker(
            "run",
            "-d",
            "--name",
            upstream,
            "--network",
            identity,
            "--network-alias",
            "logos-orchestrator",
            "--network-alias",
            "logos-webservice",
            "-v",
            f"{config}:/etc/nginx/conf.d/default.conf:ro",
            "--entrypoint",
            "nginx",
            identity,
            "-g",
            "daemon off;",
        )
        docker(
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            identity,
            "-p",
            "127.0.0.1::80",
            "-e",
            "LOGOS_INTERNAL_SECRET=" + secret,
            "-e",
            "LOGOS_AGENT_MODEL_GATEWAY_MODE=" + mode,
            identity,
        )
        port = docker("port", proxy, "80/tcp").split(":")[-1]
        base = "http://127.0.0.1:" + port
        for _ in range(100):
            try:
                with urlopen(base + "/healthz", timeout=1):
                    break
            except (URLError, TimeoutError):
                time.sleep(0.1)
        else:
            pytest.fail("Gateway did not become healthy: " + docker("logs", proxy))
        yield base, upstream
    except Exception:
        subprocess.run(["docker", "logs", proxy], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", proxy, upstream], check=False, capture_output=True)
        subprocess.run(["docker", "network", "rm", identity], check=False, capture_output=True)
        subprocess.run(["docker", "image", "rm", identity], check=False, capture_output=True)


def test_gateway_injects_credentials_hides_issuer_and_denies_revoked_identity(gateway):
    base, upstream = gateway
    request = Request(
        base + "/v1/messages",
        data=b"{}",
        headers={
            "Authorization": "Bearer attacker-key",
            "logos-key": "attacker-key",
            "logos_key": "attacker-key",
            "x-api-key": "attacker-key",
            "X-Logos-Client-Tag": "spoofed",
            "X-Logos-Agent-Inference": "cloud",
            "X-Logos-Agent-Relay-Secret": "spoofed",
        },
    )
    with urlopen(request, timeout=5) as response:
        body = json.load(response)
        assert response.headers.get("X-Logos-Agent-Token") is None
        assert response.headers.get("Authorization") is None
        assert response.headers.get("X-Logos-Agent-Relay-Secret") is None
        assert response.headers.get("X-Logos-Agent-Inference") is None
    assert body["authorization"] == "Bearer test-short-lived-token"
    assert body["path"] == "/v1/messages" and body["mode"] == "local" and body["secret"] == SECRET
    assert authorizations(upstream) == ["Bearer " + SECRET]
    assert body["alternative"] == body["anthropic"] == ""
    assert body["tag"].startswith("agent:")
    for path in ("/_agent_credential", "/internal/agent_credentials", "/v1/../internal/agent_credentials", "/v1evil"):
        with pytest.raises(HTTPError) as exc:
            urlopen(base + path, timeout=5)
        assert exc.value.code == 404
    docker("exec", upstream, "touch", "/tmp/revoked")
    with pytest.raises(HTTPError) as exc:
        urlopen(request, timeout=5)
    assert exc.value.code == 403


@pytest.mark.parametrize("gateway", [HOSTILE_SECRET], indirect=True)
def test_gateway_sends_a_secret_with_configuration_syntax_unchanged(gateway):
    base, upstream = gateway
    with urlopen(Request(base + "/v1/messages", data=b"{}"), timeout=5) as response:
        assert json.load(response)["authorization"] == "Bearer test-short-lived-token"
    assert authorizations(upstream) == ["Bearer " + HOSTILE_SECRET]


def test_gateway_refuses_a_secret_that_is_not_one_line_of_printable_ascii():
    root = Path(__file__).resolve().parents[2]
    identity = "agent-gateway-test-" + uuid.uuid4().hex[:12]
    # Use the published image's root context and allowlist, not an unfiltered hand-made context.
    docker("build", "-q", "-t", identity, "-f", str(root / "agent-gateway/Dockerfile"), str(root))
    try:
        run = subprocess.run(
            ["docker", "run", "--rm", "-e", "LOGOS_INTERNAL_SECRET=a\nproxy_pass http://evil;", identity],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert run.returncode != 0
        assert "printable ASCII" in run.stderr
    finally:
        subprocess.run(["docker", "image", "rm", identity], check=False, capture_output=True)


@pytest.mark.parametrize("gateway", [(SECRET, "remote")], indirect=True)
def test_remote_gateway_only_exposes_messages_and_replaces_control_headers(gateway):
    base, upstream = gateway
    request_headers = {
        "Authorization": "Bearer opaque-session-grant",
        "Content-Type": "application/json",
        "Logos-Key": "untrusted-key",
        "X-Api-Key": "untrusted-key",
        "X-Logos-Client-Tag": "spoofed",
        "X-Logos-Agent-Relay-Secret": "spoofed",
        "X-Logos-Agent-Inference": "local",
        "Anthropic-Version": "2023-06-01",
        "Anthropic-Beta": "test-beta",
    }
    with urlopen(Request(base + "/v1/messages", data=b"{}", headers=request_headers), timeout=5) as response:
        body = json.load(response)
        assert response.headers.get("Authorization") is None
        assert response.headers.get("X-Logos-Agent-Relay-Secret") is None
    assert body["authorization"] == "Bearer opaque-session-grant"
    assert body["secret"] == SECRET
    assert body["tag"].startswith("agent:") and body["tag"] != "spoofed"
    assert body["alternative"] == body["anthropic"] == ""
    assert body["anthropic_version"] == "2023-06-01"
    assert body["anthropic_beta"] == "test-beta"
    assert body["path"] == "/v1/messages" and body["mode"] == "cloud"
    for path in (
        "/v1/models",
        "/internal/agent-inference/messages",
        "/internal/agent_credentials",
        "/v1/chat/completions",
        "/v1/messages/count_tokens",
    ):
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(base + path, data=b"{}"), timeout=5)
        assert exc.value.code == 404
    with pytest.raises(HTTPError) as exc:
        urlopen(base + "/v1/messages", timeout=5)
    assert exc.value.code == 403
    assert authorizations(upstream) == []


def test_remote_workspace_cannot_reach_the_local_model_gateway_in_compose():
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yaml"
    env = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST") if key in os.environ}
    required = re.findall(r"\$\{([A-Z0-9_]+):\?", compose.read_text())
    env.update({key: "synthetic-test" for key in required})
    result = subprocess.check_output(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(compose),
            "--profile",
            "agent",
            "config",
            "--format",
            "json",
        ],
        env=env,
        text=True,
    )
    fixture = json.loads(result)
    services = fixture["services"]
    assert fixture["networks"]["agent"]["internal"]
    assert fixture["networks"]["agent-remote"]["internal"]
    assert set(services["logos-agent-gateway"]["networks"]) == {"internal", "agent"}
    assert set(services["logos-agent-remote-gateway"]["networks"]) == {"internal", "agent-remote"}
    assert set(services["logos-agent"]["networks"]) == {"internal"}
    assert set(services["logos-agent-egress-proxy"]["networks"]) == {"internal", "agent", "agent-remote"}
    assert not services["logos-agent-remote-gateway"].get("ports")
