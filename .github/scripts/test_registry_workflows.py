"""Exercise deployment environment generation and image credential boundaries."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


class RegistryWorkflowsTest(unittest.TestCase):
    def test_deploy_environment_ignores_legacy_and_conflicting_registry_settings(self):
        for target in ("dev", "test", "prod"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                steps = workflow(f"logos_deploy-{target}.yml")["jobs"]["deploy"]["steps"]
                script = next(step["run"] for step in steps if step["name"] == "Create .env file")
                # Run the real shell/JQ generator with no host access or real secrets.
                script = re.sub(r"\$\{\{.*?\}\}", "Test", script)
                script = script.replace("python3 scripts/", f"python3 {ROOT}/scripts/")
                env = {
                    "PATH": os.environ["PATH"],
                    "WORKSPACE_IMAGE_TAG": "pr-42",
                    "DEPLOY_TARGET": "worker",
                    "DEPLOY_SECRET_LOGOS_API_KEY": "test-only-placeholder",
                    "NODE_MANAGER": "false",
                    "DRAIN": "false",
                    "VARS_CONTEXT": json.dumps(
                        {
                            "REGISTRY": "obsolete.example/logos",
                            "IMAGE_TAG": "wrong",
                            "ENVIRONMENT": "wrong",
                            "LOGOS_HARBOR_REGISTRY": "obsolete.example",
                            "LOGOS_HARBOR_USER": "robot",
                            "KEEP_SETTING": "some value",
                        }
                    ),
                }
                subprocess.run(["bash", "-eu", "-c", script], cwd=directory, env=env, check=True)
                generated = (Path(directory) / ".env").read_text()
                self.assertEqual(generated.count("REGISTRY="), 1)
                self.assertIn("REGISTRY=ghcr.io/logososs\n", generated)
                self.assertEqual(generated.count("IMAGE_TAG="), 1)
                self.assertIn("IMAGE_TAG=pr-42\n", generated)
                self.assertNotIn("HARBOR", generated)
                self.assertNotIn("wrong", generated)
                self.assertIn('KEEP_SETTING="some value"', generated)

    def test_builds_do_not_expose_package_write_credentials_to_dockerfiles(self):
        builder = workflow("logos_build-image.yml")
        callers = workflow("logos_build-and-push-docker.yml")["jobs"]
        for job in (builder["jobs"]["build"], callers["build-workernode-mlx"]):
            steps = job["steps"]
            checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
            self.assertIs(checkout["with"]["persist-credentials"], False)
            build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
            self.assertEqual(build["with"]["github-token"], "")
            for secret_input in ("secrets", "secret-envs", "secret-files"):
                self.assertNotIn(secret_input, build["with"])
            self.assertNotIn("secrets.", json.dumps(build))
            meta = next(s for s in steps if s.get("uses", "").startswith("docker/metadata-action@"))
            latest = next(line for line in meta["with"]["tags"].splitlines() if "value=latest" in line)
            self.assertIn("github.event_name == 'push' && github.ref == 'refs/heads/main'", latest)

    def test_cleanup_covers_all_published_packages_and_uses_trusted_code(self):
        published = {
            job["with"]["image"]
            for job in workflow("logos_build-and-push-docker.yml")["jobs"].values()
            if "uses" in job
        }
        published.add("logos-workernode-mlx")
        script = ROOT / ".github/scripts/cleanup-ghcr.cjs"
        output = subprocess.check_output(
            ["node", "-e", "console.log(JSON.stringify(require(process.argv[1]).packages))", str(script)], text=True
        )
        self.assertEqual(set(json.loads(output)), published)
        cleanup = workflow("logos_cleanup-pr-builds.yml")
        checkout = cleanup["jobs"]["cleanup"]["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "${{ github.event.repository.default_branch }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertNotIn("pull_request.head.sha", json.dumps(cleanup))


if __name__ == "__main__":
    unittest.main()
