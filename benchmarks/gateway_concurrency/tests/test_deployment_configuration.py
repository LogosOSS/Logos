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
