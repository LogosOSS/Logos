"""Report candidate platform versions for maintainer review."""

import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from string import Template
from urllib.request import urlopen


def latest_release(repository):
    return subprocess.check_output(
        ["gh", "api", f"repos/{repository}/releases/latest", "--jq", ".tag_name"], text=True
    ).strip()


def npm_version(package):
    with urlopen(f"https://registry.npmjs.org/{package}/latest", timeout=30) as response:
        return json.load(response)["version"]


def setup_version(tool, release_line):
    url = f"https://raw.githubusercontent.com/actions/{tool}-versions/main/versions-manifest.json"
    with urlopen(url, timeout=30) as response:
        versions = [
            item["version"]
            for item in json.load(response)
            if item["stable"] and item["version"].startswith(release_line + ".")
        ]
    return max(versions, key=lambda version: tuple(map(int, version.split("."))))


def workspace_tools():
    workspace = Path("logos-agent/workspace/Dockerfile").read_text()
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", workspace, re.MULTILINE))
    jdk = re.search(r"^ENV JAVA_VERSION=(\S+)$", workspace, re.MULTILINE)[1]
    major = args["JDK_VERSION"]
    yield "Temurin", jdk, latest_release(f"adoptium/temurin{major}-binaries")
    yield "GitHub CLI", args["GH_VERSION"], latest_release("cli/cli").removeprefix("v")
    yield "Claude Code", args["CLAUDE_CODE_VERSION"], npm_version("@anthropic-ai/claude-code")
    yield "Playwright", args["PLAYWRIGHT_VERSION"], npm_version("playwright")
    node = Path("logos-ui/.nvmrc").read_text().strip()
    yield "Node.js (CI)", node, setup_version("node", node.split(".")[0])
    ci = Path(".github/workflows/logos_test.yml").read_text()
    python = re.search(r'python-version: "(\d+\.\d+\.\d+)"', ci)[1]
    yield "Python (CI)", python, setup_version("python", python.rsplit(".", 1)[0])
    uv = re.search(r"^FROM ghcr.io/astral-sh/uv:([\d.]+)", Path("logos-workernode/Dockerfile").read_text(), re.M)[1]
    yield "uv (images and CI)", uv, latest_release("astral-sh/uv")


def apt_inputs():
    """Collect literal apt pins with the base image whose repositories supply them."""
    images = defaultdict(set)
    paths = subprocess.check_output(["git", "ls-files", "*Dockerfile*"], text=True).splitlines()
    for path in paths:
        instructions = re.sub(r"(?m)^[ \t]*#[^\n]*\n", "", Path(path).read_text())
        args, stages = {}, {}
        for line in instructions.replace("\\\n", " ").splitlines():
            if arg := re.match(r"ARG (\w+)=(\S+)", line):
                args[arg[1]] = arg[2]
            if stage := re.match(r"FROM (\S+)(?: AS (\S+))?", line, re.IGNORECASE):
                base = Template(stage[1]).safe_substitute(args)
                image = stages.get(base, base)
                if stage[2]:
                    stages[stage[2]] = image
            for install in re.findall(r"\bapt-get install\s+([^&;|]+)", line):
                pins = re.findall(r"(?:^|\s)([a-z0-9][a-z0-9+.-]*=[0-9][\w:.+~\-]*)", install)
                if pins:
                    images[image].update(pins)
    return images


APT_QUERY = """
set -eu
apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=2 -qq update >&2
for pin do
    package=${pin%%=*}
    installed=${pin#*=}
    candidate=$(apt-cache policy "$package" | awk '/Candidate:/ {print $2}')
    if [ -z "$candidate" ] || [ "$candidate" = "(none)" ]; then
        echo "| $package | $installed | unavailable |"
    elif dpkg --compare-versions "$candidate" gt "$installed"; then
        echo "| $package | $installed | $candidate |"
    fi
done
"""


def report():
    print("### Workspace tools\n\n| Tool | Pinned | Latest stable |\n| --- | --- | --- |")
    for name, pinned, latest in workspace_tools():
        print(f"| {name} | {pinned} | {latest} |")
    print("\nReview Temurin archives, workspace packages, both architectures' checksums and CI tool versions together.")

    print("\n### Distribution packages (linux/amd64)")
    for image, pins in sorted(apt_inputs().items()):
        rows = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--entrypoint",
                "sh",
                image,
                "-c",
                APT_QUERY,
                "sh",
                *sorted(pins),
            ],
            text=True,
        ).strip()
        print(f"\n**{image.split('@')[0]}** — {len(pins)} pins checked.\n")
        if rows:
            print("| Package | Pinned | Candidate |\n| --- | --- | --- |")
            print(rows)
    print(
        "\nFor an update, select the archive snapshot and base digest together and rebuild on supported architectures."
    )

    lock = Path("logos-workernode/requirements.lock").read_text()
    print("\n### GPU worker toolchain\n")
    for name, version in re.findall(r"^(vllm|torch|nvidia-cuda-(?:runtime|nvcc|crt)|nvidia-nvvm)==(\S+)", lock, re.M):
        print(f"- {name} {version}")
    print(
        "\nThe scheduled vLLM and vLLM Metal updates own the release and wheel pins. "
        "The lock generator matches compiler wheels to the runtime CUDA minor."
    )


if __name__ == "__main__":
    report()
