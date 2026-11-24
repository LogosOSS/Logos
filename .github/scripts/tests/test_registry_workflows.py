"""Exercise image build and cleanup credential boundaries."""

import json
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


class RegistryWorkflowsTest(unittest.TestCase):
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
