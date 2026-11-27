"""Check the environment that Compose actually gives the gateway container."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is required")
class DeploymentConfigurationTest(unittest.TestCase):
    def test_gateway_capacity_reaches_production_and_development_containers(self):
        root = Path(__file__).resolve().parents[3]
        for compose in ("docker-compose.yaml", "docker-compose.dev.yaml"):
            for value, expected in ((None, "0"), ("", "0"), ("0", "0"), ("128", "128")):
                with self.subTest(compose=compose, value=value):
                    env = dict(os.environ)
                    env.update(
                        LOGOS_DB_PASSWORD="configuration-test",
                        LOGOS_REDIS_PASSWORD="configuration-test",
                        KEYCLOAK_ISSUER_URI="https://example.invalid/realms/test",
                    )
                    env.pop("LOGOS_GATEWAY_MAX_IN_FLIGHT", None)
                    if value is not None:
                        env["LOGOS_GATEWAY_MAX_IN_FLIGHT"] = value
                    result = subprocess.run(
                        ["docker", "compose", "--env-file", os.devnull, "-f", compose, "config", "--format", "json"],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    service = json.loads(result.stdout)["services"]["logos-webservice"]
                    self.assertEqual(service["environment"]["LOGOS_GATEWAY_MAX_IN_FLIGHT"], expected)

    def test_all_stacks_use_the_same_ephemeral_valkey_image(self):
        root = Path(__file__).resolve().parents[3]
        env = {
            **os.environ,
            "LOGOS_DB_PASSWORD": "configuration-test",
            "LOGOS_REDIS_PASSWORD": "valkey-configuration-test",
            "KEYCLOAK_ISSUER_URI": "https://example.invalid/realms/test",
        }
        images = set()
        for compose in (
            "docker-compose.yaml",
            "docker-compose.dev.yaml",
            "e2e/harness/stack/docker-compose.e2e.yaml",
        ):
            with self.subTest(compose=compose):
                result = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "--env-file",
                        os.devnull,
                        "-f",
                        compose,
                        "--profile",
                        "ui",
                        "config",
                        "--format",
                        "json",
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                services = json.loads(result.stdout)["services"]
                store = services["logos-redis"]
                webservice = services["logos-webservice"]
                images.add(store["image"])
                self.assertRegex(store["image"], r"^valkey/valkey:\d+\.\d+\.\d+-alpine@sha256:[a-f0-9]{64}$")
                self.assertEqual(store["command"][:5], ["valkey-server", "--save", "", "--appendonly", "no"])
                self.assertIn("/data", store["tmpfs"])
                self.assertNotIn("ports", store)
                self.assertEqual(store["healthcheck"]["test"], ["CMD", "valkey-cli", "-e", "ping"])
                self.assertEqual(webservice["depends_on"]["logos-redis"]["condition"], "service_healthy")
                self.assertEqual(webservice["environment"]["REDIS_HOST"], "logos-redis")
                if compose == "docker-compose.yaml":
                    password = env["LOGOS_REDIS_PASSWORD"]
                    self.assertEqual(store["command"][5:], ["--requirepass", password])
                    self.assertEqual(store["environment"]["VALKEYCLI_AUTH"], password)
                    self.assertEqual(webservice["environment"]["REDIS_PASSWORD"], password)
        self.assertEqual(len(images), 1)
