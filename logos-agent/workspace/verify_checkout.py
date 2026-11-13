#!/usr/bin/env python3
"""Compile changed Java/Angular services in a credential-free container.

The runner supplies changed paths from its trusted Git snapshot and reads the
container exit status. The checkout is mounted read-only; repository build
code executes only against a disposable copy with private dependency caches.
An agent can also invoke this script with service names before finishing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

CHECKOUT = Path("/workspace/repo")
CACHE = Path("/opt/logos-verification")
SERVICES = ("logos-webservice", "logos-ui")
COMMAND_TIMEOUT = 1800


def selected_services(paths: list[str]) -> list[str]:
    """Select supported services without interpreting paths as commands."""
    selected = set()
    for value in paths:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("verification paths must be strings without NUL bytes")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("verification paths must be repository-relative")
        if path.parts[0] in SERVICES:
            selected.add(path.parts[0])
    return [service for service in SERVICES if service in selected]


def require_credential_free() -> None:
    """Refuse accidental use in a credential-bearing helper phase."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "LOGOS_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(name):
            raise RuntimeError(f"verification refuses an environment containing {name}")
    auth = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if auth and auth != "injected-by-logos-agent-gateway":
        raise RuntimeError("verification refuses an environment containing ANTHROPIC_AUTH_TOKEN")
    if os.environ.get("LOGOS_SESSION_PHASE") in ("prepare", "snapshot", "finalize", "ssh-trust"):
        raise RuntimeError("verification cannot execute in a trusted helper phase")
    if Path("/logos/deploy_key").exists():
        raise RuntimeError("verification cannot execute with a deploy key mounted")


def _environment(home: Path) -> dict[str, str]:
    # Do not inherit the agent's HOME, Maven/npm startup options, Git helpers,
    # proxies, or model endpoint. Build scripts get only their own scratch home.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "MAVEN_OPTS": f"-Duser.home={home}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "true",
        "NG_CLI_ANALYTICS": "false",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "npm_config_offline": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    }
    if os.environ.get("JAVA_HOME"):
        environment["JAVA_HOME"] = os.environ["JAVA_HOME"]
    return environment


def _run(command: list[str], directory: Path, environment: dict[str, str]) -> None:
    print(f"[verify] {directory.name}: {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=directory, env=environment, check=True, timeout=COMMAND_TIMEOUT)


def verify_service(service: str, checkout: Path, cache: Path) -> None:
    source = checkout / service
    if service not in SERVICES or source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"cannot verify missing or linked service directory: {service}")
    with tempfile.TemporaryDirectory(prefix="logos-verify-") as directory:
        scratch = Path(directory)
        home = scratch / "home"
        home.mkdir()
        working = scratch / service

        def generated_outputs(directory: str, entries: list[str]) -> set[str]:
            outputs = {".git"}
            if Path(directory) == source:
                outputs.update(("node_modules", "target", "dist", ".angular", "coverage"))
            return outputs.intersection(entries)

        shutil.copytree(
            source,
            working,
            symlinks=True,
            ignore=generated_outputs,
        )
        environment = _environment(home)
        if service == "logos-webservice":
            repository = scratch / "maven-repository"
            shutil.copytree(cache / "maven-repository", repository)
            _run(
                [
                    "mvn",
                    "--offline",
                    "--batch-mode",
                    "--no-transfer-progress",
                    f"-Dmaven.repo.local={repository}",
                    "test-compile",
                ],
                working,
                environment,
            )
        else:
            npm_cache = scratch / "npm-cache"
            shutil.copytree(cache / "npm-cache", npm_cache)
            _run(
                ["npm", "ci", "--offline", "--no-audit", "--no-fund", "--cache", str(npm_cache)],
                working,
                environment,
            )
            angular = ["node", "node_modules/@angular/cli/bin/ng.js"]
            _run([*angular, "build", "--configuration", "production"], working, environment)
            _run([*angular, "test", "--watch=false"], working, environment)


def main(arguments: list[str] | None = None) -> int:
    try:
        require_credential_free()
        arguments = sys.argv[1:] if arguments is None else arguments
        if arguments:
            if any(service not in SERVICES for service in arguments):
                raise ValueError(f"explicit services must be one of: {', '.join(SERVICES)}")
            paths = arguments
        else:
            paths = json.loads(os.environ.get("LOGOS_VERIFICATION_PATHS", "null"))
            if not isinstance(paths, list):
                raise ValueError("LOGOS_VERIFICATION_PATHS must be a JSON array from the runner")
        services = selected_services(paths)
        if not services:
            print("[verify] No changed Java or Angular service; no compile check selected.", flush=True)
        failed = False
        for service in services:
            try:
                verify_service(service, CHECKOUT, CACHE)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"[verify] FAILED {service}: {exc}", file=sys.stderr, flush=True)
                failed = True
            else:
                print(f"[verify] PASSED {service}", flush=True)
        return 1 if failed else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[verify] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
