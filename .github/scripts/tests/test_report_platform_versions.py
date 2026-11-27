import io
import json
import runpy
import subprocess
import urllib.request
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "report_platform_versions.py"


def test_platform_report_uses_matching_release_lines_and_package_repositories(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    files = {
        "logos-agent/workspace/Dockerfile": (
            "ARG JDK_VERSION=25\nENV JAVA_VERSION=jdk-25+1\nARG GH_VERSION=1.0\n"
            "ARG BASE=debian:stable\nFROM ${BASE} AS build\nRUN apt-get install curl=1.2 libx\n"
            "FROM build\nRUN apt-get install curl=1.2 \\\n  git=2.0\n# RUN apt-get install ignored=1\n"
        ),
        "logos-agent/workspace/package.json": json.dumps(
            {"dependencies": {"@anthropic-ai/claude-code": "2.1.1", "playwright": "3.2.1"}}
        ),
        "logos-ui/.nvmrc": "22.1.2",
        ".github/workflows/logos_test.yml": 'python-version: "3.13.2"',
        "logos-workernode/Dockerfile": (
            "FROM ghcr.io/astral-sh/uv:0.9.1 AS uv\nFROM ubuntu\nRUN apt-get install git=3\n"
        ),
        "logos-workernode/requirements.lock": "torch==2.1\nnvidia-cuda-nvcc==13.1\nother==1\n",
    }
    for name, content in files.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def upstream(url, timeout):
        if "registry.npmjs.org" in url:
            data = {
                "https://registry.npmjs.org/@anthropic-ai/claude-code/stable": {"version": "2.2.0"},
                "https://registry.npmjs.org/playwright/latest": {"version": "4.0.0"},
            }[url]
        else:
            line = {"node-versions": "22.1", "python-versions": "3.13"}[url.split("/")[4]]
            releases = [(line + ".9", True), (line + ".10", True), (line + ".11", False), ("99.0.0", True)]
            data = [{"version": version, "stable": stable} for version, stable in releases]
        return io.BytesIO(json.dumps(data).encode())

    def command(args, *, text):
        if args[0] == "git":
            return "\n".join(name for name in files if name.endswith("Dockerfile"))
        if args[0] == "gh":
            return {
                "repos/adoptium/temurin25-binaries/releases/latest": "jdk-25+2",
                "repos/cli/cli/releases/latest": "v1.1",
                "repos/astral-sh/uv/releases/latest": "0.9.2",
            }[args[2]]
        assert args[:7] == ["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "sh"]
        if args[7] == "debian:stable":
            assert args[11:] == ["curl=1.2", "git=2.0"]
            return "| curl | 1.2 | 1.3 |"
        assert args[7] == "ubuntu"
        assert args[11:] == ["git=3"]
        return ""

    monkeypatch.setattr(urllib.request, "urlopen", upstream)
    monkeypatch.setattr(subprocess, "check_output", command)
    runpy.run_path(str(SCRIPT), run_name="__main__")
    output = capsys.readouterr().out
    for row in (
        "| Temurin | jdk-25+1 | jdk-25+2 |",
        "| GitHub CLI | 1.0 | 1.1 |",
        "| Claude Code | 2.1.1 | 2.2.0 |",
        "| Playwright | 3.2.1 | 4.0.0 |",
        "| Node.js (CI) | 22.1.2 | 22.1.10 |",
        "| Python (CI) | 3.13.2 | 3.13.10 |",
        "| uv (images and CI) | 0.9.1 | 0.9.2 |",
        "| curl | 1.2 | 1.3 |",
        "- torch 2.1",
        "- nvidia-cuda-nvcc 13.1",
    ):
        assert row in output
    assert "Dependabot opens update PRs for workspace npm packages" in output
    assert output.count("| Package | Pinned | Candidate |") == 1
    assert "other" not in output
