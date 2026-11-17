"""The repository-wide guard against installs without exact versions."""

import runpy
import subprocess
from pathlib import Path

import pytest

GUARD = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/check-pinned-installs.py"))
check_command = GUARD["check_command"]
check_from = GUARD["check_from"]
check_file = GUARD["check_file"]
main = GUARD["main"]
EXEMPT = GUARD["EXEMPT"]


@pytest.mark.parametrize(
    "command",
    [
        "uv pip install --no-deps --require-hashes -r requirements.lock",
        "pip install --no-deps --require-hashes -r tools/pre-commit/requirements.lock",
        '"$VENV/bin/python" -m pip install --quiet --no-deps --require-hashes -r "$ROOT/x.lock"',
        "uv pip install --python .venv/bin/python --no-deps --no-build-isolation -e .",
        'uv pip install --no-cache --reinstall --no-deps "torch==${torch_ver}"',
        "uv sync --locked --extra dev --no-install-project --python 3.13",
        "apt-get install -y --no-install-recommends curl=8.14.1-2+deb13u7 gcc=4:14.2.0-1",
        'apt-get install -y "gh=${GH_VERSION}"',
        "npm ci",
        'npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" playwright@1.62.1',
        "npx --yes playwright@1.62.1 install chromium",
        'uvx --quiet "uv@$UV_VERSION" pip compile x.in',
        "uv python install 3.13.18 3.12.16",
        "pip install 'black[jupyter]==26.10.1' isort==5.13.2",
        "uv tool install ruff==0.15.2",
        "pipx install pre-commit==4.6.2",
        "sudo env FOO=1 pip install requests==2.32.5",
        "python3 -m pip install --no-deps ./dist/pkg.whl",
        "go install golang.org/x/tools/gopls@v0.20.0",
        "npx --no-install tsc",
    ],
)
def test_pinned_installs_pass(command: str) -> None:
    assert check_command(command) == []


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("pip install pre-commit", "pip"),
        ("uv pip install -r requirements.txt", "pip"),
        ("uv pip install .", "pip"),
        ('uv pip install -e ".[dev]"', "pip"),
        ("uv pip install --upgrade transformers", "pip"),
        ("uv sync", "uv-sync"),
        ("uv sync --frozen", "uv-sync"),
        ("apt-get update && apt-get install -y curl", "apt"),
        ("apk add --no-cache bash", "apk"),
        ("npm install", "npm"),
        ("npm install -g typescript", "npm"),
        ("npx playwright install chromium", "npx"),
        ("uvx ruff check", "uvx"),
        ("uv python install 3.13", "uv-python"),
        ("brew install uv", "brew"),
        ("curl -fsSL https://example.invalid/install.sh | bash", "curl-pipe"),
        ("pip install 'requests==2.*'", "pip"),
        ("pip install 'requests==2.32.5,<3'", "pip"),
        ("pip install 'requests>=2.32'", "pip"),
        ("pip install 'requests===2.32.5'", "pip"),
        ("python3 -m pip install ./local-project", "pip"),
        ("uv tool install 'ruff==0.*'", "tool"),
        ("pipx install pre-commit", "tool"),
        ("uvx ruff@latest check", "uvx"),
        ("uv tool run black .", "uvx"),
        ("npm install -g typescript@5.x", "npm"),
        ("npm i -g typescript@latest", "npm"),
        ("npx playwright@^1.62.1 install", "npx"),
        ("cargo install ripgrep", "cargo"),
        ("gem install bundler", "gem"),
    ],
)
def test_unpinned_installs_fail(command: str, rule: str) -> None:
    problems = check_command(command)
    assert problems and problems[0].split(":", 1)[0] == rule


@pytest.mark.parametrize("line", ["FROM nginx:alpine", "FROM python", "FROM node:latest"])
def test_unversioned_images_fail(line: str) -> None:
    assert check_from(line, {"__stages__": ""})


def test_versioned_images_and_stages_pass() -> None:
    assert check_from("FROM python:3.13-slim-trixie@sha256:" + "0" * 64 + " AS builder", {"__stages__": ""}) is None
    assert check_from("FROM builder", {"__stages__": " builder"}) is None
    assert check_from("FROM ${BASE_IMAGE}", {"__stages__": "", "BASE_IMAGE": "ubuntu:24.04@sha256:" + "0" * 64}) is None


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def rules(problems: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(number, problem.split(":", 1)[0]) for number, problem in problems]


def test_dockerfile_run_instructions_are_checked(tmp_path: Path) -> None:
    dockerfile = write(
        tmp_path,
        "svc/Dockerfile",
        "ARG BASE=python:3.13-slim@sha256:" + "0" * 64 + "\n"
        "FROM ${BASE} AS build\n"
        "# RUN pip install ignored-in-a-comment\n"
        "RUN pip install requests\n"
        "RUN --mount=type=cache,target=/root/.cache \\\n"
        "    # a comment inside the continuation\n"
        "    npm install -g typescript\n"
        "RUN pip install requests==2.32.5\n"
        "FROM build\n"
        "FROM nginx:alpine\n",
    )
    assert rules(check_file(dockerfile, tmp_path)) == [(4, "pip"), (5, "npm"), (10, "from")]


def test_workflow_run_values_and_setup_steps_are_checked(tmp_path: Path) -> None:
    workflow = write(
        tmp_path,
        ".github/workflows/ci.yml",
        """jobs:
  test:
    steps:
      - uses: astral-sh/setup-uv@v7
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.13.6"
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
      - uses: actions/setup-node@v6
        with:
          node-version-file: .nvmrc
      - uses: actions/setup-java@v5
        with:
          java-version: 25.0.5+8
      - run: pip install requests
      - name: Inline quoted
        run: "npm install -g typescript"
      - run: |
          pip install --no-deps --require-hashes -r requirements.lock
          uv sync
""",
    )
    assert rules(check_file(workflow, tmp_path)) == [
        (17, "pip"),
        (19, "npm"),
        (22, "uv-sync"),
        (4, "setup"),
        (8, "setup"),
    ]


def test_yaml_outside_workflows_skips_setup_steps(tmp_path: Path) -> None:
    compose = write(tmp_path, "compose.yml", "steps:\n  - uses: actions/setup-python@v6\n")
    assert check_file(compose, tmp_path) == []


def test_package_json_scripts_are_checked(tmp_path: Path) -> None:
    package = write(
        tmp_path,
        "ui/package.json",
        '{"scripts": {"setup": "npm install", "e2e": "playwright install --with-deps chromium", "ok": "npm ci"}}',
    )
    problems = check_file(package, tmp_path)
    assert rules(problems) == [(0, "npm"), (0, "apt")]
    assert "(script 'setup')" in problems[0][1] and "(script 'e2e')" in problems[1][1]


def test_invalid_package_json_is_ignored(tmp_path: Path) -> None:
    assert check_file(write(tmp_path, "package.json", "{not json"), tmp_path) == []


def test_markdown_checks_commands_but_shell_skips_comments(tmp_path: Path) -> None:
    doc = write(tmp_path, "README.md", "# Setup\n\n```bash\npip install requests\n```\n")
    script = write(tmp_path, "run.sh", "#!/bin/sh\n# pip install requests\nset -e\n")
    assert rules(check_file(doc, tmp_path)) == [(4, "pip")]
    assert check_file(script, tmp_path) == []


def test_exemptions_drop_only_the_named_rule(tmp_path: Path) -> None:
    rel = "logos-workernode/scripts/bootstrap-macos.sh"
    assert (rel, "brew") in EXEMPT
    script = write(tmp_path, rel, "brew install uv\npip install requests\n")
    assert rules(check_file(script, tmp_path)) == [(2, "pip")]


def git_repo(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in files.items():
        write(root, name, text)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)


def test_main_reports_violations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git_repo(
        tmp_path,
        {
            "Dockerfile": "FROM python:3.13-slim\nRUN pip install requests\n",
            "package.json": '{"scripts": {"setup": "npm install"}}',
            "notes.txt": "pip install requests\n",
            "node_modules/x/run.sh": "pip install requests\n",
        },
    )
    assert main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "Dockerfile:2: pip:" in out
    assert "package.json: npm:" in out
    assert "notes.txt" not in out and "node_modules" not in out
    assert "2 install(s) without an exact version" in out


def test_main_passes_on_pinned_installs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git_repo(tmp_path, {"setup.sh": "pip install --no-deps --require-hashes -r requirements.lock\n"})
    assert main(tmp_path) == 0
    assert "Every install pins an exact version." in capsys.readouterr().out
