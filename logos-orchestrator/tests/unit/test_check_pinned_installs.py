"""The repository-wide guard against installs without exact versions."""

import runpy
from pathlib import Path

import pytest

GUARD = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/check-pinned-installs.py"))
check_command = GUARD["check_command"]
check_from = GUARD["check_from"]


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
