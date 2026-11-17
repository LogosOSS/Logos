#!/usr/bin/env python3
"""Fail on any install command in the repository that does not pin exact versions.

Nothing may be installed without an exact version. This check scans every
Dockerfile, shell script, GitHub workflow, Markdown document and package.json
script in the repository (tracked files) and rejects:

- Python installs that resolve: `pip install` / `uv pip install` must install a
  hash-pinned lock (`--require-hashes -r *.lock`), exact `name==1.2.3` pins
  (no wildcards or ranges), or a local project with `--no-deps`; `uv sync` must
  pass `--locked`.
- `uv tool install` / `pipx install` / `uvx` without an exact version.
- `apt-get install` / `apt install` / `apk add` packages without `=version`.
- `npm install` / `npm i` (use `npm ci`), and global installs or `npx` calls
  without an exact `@version`.
- `brew install` (Homebrew cannot pin versions) and `curl … | sh` installers.
- `uv python install` without an exact x.y.z version.
- Dockerfile `FROM` images without a version tag (or with `latest`).
- `astral-sh/setup-uv` without `version:`, and setup-python / setup-java /
  setup-node without an exact version.

Deliberate exceptions are listed in EXEMPT below, each with its reason; keep
that list short and reviewed.

Usage: scripts/check-pinned-installs.py   (exit 1 and a report on violations)
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (path, rule) → reason. A rule name matches the first word of a violation.
EXEMPT: dict[tuple[str, str], str] = {
    # The macOS bootstrap installs the host prerequisites through Homebrew,
    # which has no version pinning for formulae; the worker's own Python
    # environment is installed from requirements-standalone.lock.
    ("logos-workernode/scripts/bootstrap-macos.sh", "brew"): "Homebrew cannot pin formula versions",
    ("logos-workernode/scripts/bootstrap-macos.sh", "curl-pipe"): "Homebrew's official installer",
    ("logos-workernode/scripts/install-macos.sh", "brew"): "error messages naming the Homebrew prerequisites",
    ("logos-workernode/MACOS.md", "brew"): "documents the Homebrew prerequisites of the macOS worker",
    ("logos-workernode/QUICKSTART.md", "brew"): "documents the Homebrew prerequisites of the macOS worker",
}

SUFFIXES = {".sh", ".yml", ".yaml", ".md", ".mdx"}
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+([.+-][0-9A-Za-z.+-]*)?$")
# `name==version` (or `name[extra]==version`) with a concrete version: no `*`
# wildcard, no second specifier. A shell variable stands for a version the
# script determines itself.
PINNED_PYTHON_SPEC = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]*\])?" r"==(\d[0-9A-Za-z.+!_-]*|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)$"
)
# `pkg@1.2.3` (npm, also scoped) or a version from a shell variable.
PINNED_NPM_SPEC = re.compile(r".@(\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)$")
# `tool@1.2.3` for uvx, or a version from a shell variable.
PINNED_UVX_SPEC = re.compile(r".@(\d[0-9A-Za-z.+!_-]*|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)$")
# Options of pip / uv pip install that take a value.
PIP_VALUE_OPTIONS = {
    "-r",
    "--requirement",
    "-c",
    "--constraint",
    "--python",
    "-p",
    "--index-url",
    "-i",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--target",
    "--prefix",
    "--cache-dir",
    "--python-version",
    "--python-platform",
    "--root",
    "--override",
}
# Variables a Dockerfile validates as exact pins before installing them.
VALIDATED_SPEC_VARIABLES = {"$NCCL_PIP_SPEC", "${NCCL_PIP_SPEC}"}


def tracked_files(root: Path = ROOT) -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True).stdout
    files = []
    for name in out.splitlines():
        path = root / name
        base = path.name
        if "node_modules" in path.parts or not path.is_file():
            continue
        if base.startswith("Dockerfile") or path.suffix in SUFFIXES or base == "package.json":
            files.append(path)
    return files


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations; drop full-line comments inside them."""
    lines: list[tuple[int, str]] = []
    buf, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if buf and stripped.startswith("#"):
            continue
        if not buf:
            start = number
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        lines.append((start, buf))
        buf = ""
    if buf:
        lines.append((start, buf))
    return lines


def segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||;|\$\(|`|\)", command) if s.strip()]


def tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def after(words: list[str], *keyword: str) -> list[str] | None:
    n = len(keyword)
    for i in range(len(words) - n + 1):
        if tuple(words[i : i + n]) == keyword:
            return words[i + n :]
    return None


def pip_violation(args: list[str]) -> str | None:
    if "--require-hashes" in args or "--offline" in args:
        return None
    specs, files, i = [], [], 0
    while i < len(args):
        arg = args[i]
        if arg in ("|", ">", ">>", "2>&1"):
            break
        if arg in ("-r", "--requirement"):
            files.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if arg in PIP_VALUE_OPTIONS:
            i += 2
            continue
        if arg in ("-e", "--editable"):
            specs.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        specs.append(arg)
        i += 1
    if files:
        return "pip installs a requirements file without --require-hashes: " + " ".join(files)
    for spec in specs:
        if spec in VALIDATED_SPEC_VARIABLES:
            continue
        if spec in (".", "./") or spec.startswith(("./", "../", "/")) or spec.endswith((".whl", ".lock")):
            if "--no-deps" not in args and not spec.endswith(".whl"):
                return f"pip installs local project {spec!r} without --no-deps (resolves its dependencies)"
            continue
        if not PINNED_PYTHON_SPEC.match(spec):
            return f"pip installs {spec!r} without an exact == version"
    return None


def check_command(command: str) -> list[str]:
    problems: list[str] = []
    if re.search(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", command):
        problems.append("curl-pipe: piping a downloaded script into a shell installs whatever it fetches")
    for segment in segments(command):
        words = tokens(segment)
        if not words:
            continue
        # Skip assignments and sudo/env prefixes.
        while words and (words[0] in ("sudo", "env", "exec") or re.match(r"^[A-Z_][A-Z0-9_]*=", words[0])):
            words = words[1:]
        if not words:
            continue
        text = " ".join(words)
        pip_args = None
        if words[:3] == ["uv", "pip", "install"]:
            pip_args = words[3:]
        elif words[0] in ("pip", "pip3") and words[1:2] == ["install"]:
            pip_args = words[2:]
        elif re.search(r"python[0-9.]*$", words[0]) and words[1:4] == ["-m", "pip", "install"]:
            pip_args = words[4:]
        if pip_args is not None:
            problem = pip_violation(pip_args)
            if problem:
                problems.append("pip: " + problem)
        rest = after(words, "uv", "sync")
        if rest is not None and "--locked" not in rest:
            problems.append("uv-sync: `uv sync` without --locked may resolve a different set than uv.lock")
        for prefix in (("uv", "tool", "install"), ("pipx", "install")):
            rest = after(words, *prefix)
            if rest is not None:
                specs = [w for w in rest if not w.startswith("-")]
                if not specs or not PINNED_PYTHON_SPEC.match(specs[0]):
                    problems.append("tool: " + " ".join(prefix) + " without an exact == version")
        if words[0] == "uvx" or after(words, "uv", "tool", "run") is not None:
            rest = words[1:] if words[0] == "uvx" else after(words, "uv", "tool", "run")
            target = next((w for w in rest if not w.startswith("-")), "")
            if not PINNED_UVX_SPEC.search(target) and not PINNED_PYTHON_SPEC.match(target):
                problems.append(f"uvx: {target!r} without an exact version (use tool@x.y.z)")
        rest = after(words, "uv", "python", "install")
        if rest is not None:
            versions = [w for w in rest if not w.startswith("-")]
            if not versions or not all(EXACT_VERSION.match(v) for v in versions):
                problems.append("uv-python: `uv python install` without exact x.y.z versions")
        for prefix in (("apt-get", "install"), ("apt", "install")):
            rest = after(words, *prefix)
            if rest is not None:
                bad = [w for w in rest if not w.startswith("-") and "=" not in w and w not in ("|", ">")]
                if bad:
                    problems.append("apt: packages without =version: " + " ".join(bad))
        rest = after(words, "apk", "add")
        if rest is not None:
            bad = [w for w in rest if not w.startswith("-") and "=" not in w]
            if bad:
                problems.append("apk: packages without =version: " + " ".join(bad))
        if words[0] == "npm" and len(words) > 1 and words[1] in ("install", "i", "add"):
            pkgs = [w for w in words[2:] if not w.startswith("-")]
            if not pkgs:
                problems.append("npm: `npm install` may rewrite the lockfile; use `npm ci`")
            for pkg in pkgs:
                if not PINNED_NPM_SPEC.search(pkg):
                    problems.append(f"npm: {pkg!r} without an exact @version")
        if words[0] == "npx":
            target = next((w for w in words[1:] if not w.startswith("-")), "")
            if not PINNED_NPM_SPEC.search(target) and "--no-install" not in words:
                problems.append(f"npx: {target!r} without an exact @version")
        if after(words, "brew", "install") is not None:
            problems.append("brew: Homebrew cannot install exact versions")
        if words[0] in ("go", "cargo", "gem") and len(words) > 1 and words[1] == "install":
            if not re.search(r"(@v?\d|--version|-v\s)", text):
                problems.append(f"{words[0]}: install without an exact version")
    return problems


def shell_command(line: str, *, dockerfile: bool, workflow: bool) -> str:
    """Return the shell command of a Dockerfile RUN or a workflow `run:` line."""
    if dockerfile:
        m = re.match(r"^RUN\s+(--\S+\s+)*", line, re.I)
        if m:
            return line[m.end() :]
    if workflow:
        m = re.match(r"^(-\s+)?run:\s*", line)
        if m:
            command = line[m.end() :].strip()
            if len(command) > 1 and command[0] == command[-1] and command[0] in "'\"":
                command = command[1:-1]
            return command
    return line


def check_from(line: str, args: dict[str, str]) -> str | None:
    m = re.match(r"^FROM\s+(--platform=\S+\s+)?(\S+)", line, re.I)
    if not m:
        return None
    image = m.group(2)
    var = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", image)
    if var:
        image = args.get(var.group(1), "")
        if not image:
            return None  # a build stage name or an ARG without default is resolved elsewhere
    if image == "scratch" or image in args.get("__stages__", "").split():
        return None
    name = image.split("@", 1)[0]
    tag = name.rsplit(":", 1)[1] if ":" in name.rsplit("/", 1)[-1] else ""
    if not tag or tag == "latest" or not re.search(r"\d", tag):
        return f"from: image {image!r} has no version tag"
    return None


def check_workflow_steps(text: str) -> list[tuple[int, str]]:
    problems = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"uses:\s*(astral-sh/setup-uv|actions/setup-(python|java|node))@", line)
        if not m:
            continue
        indent = len(line) - len(line.lstrip(" -"))
        block = []
        for nxt in lines[i + 1 : i + 12]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent - 2 and nxt.lstrip().startswith("-"):
                break
            block.append(nxt)
        joined = "\n".join(block)
        if m.group(1) == "astral-sh/setup-uv":
            if not re.search(r'^\s*version:\s*"?\d+\.\d+\.\d+"?\s*$', joined, re.M):
                problems.append((i + 1, "setup: astral-sh/setup-uv without an exact `version:`"))
            continue
        key = {"python": "python-version", "java": "java-version", "node": "node-version"}[m.group(2)]
        vm = re.search(rf'^\s*{key}:\s*"?([^"\s]+)"?\s*$', joined, re.M)
        if vm is None and re.search(rf"^\s*{key}-file:", joined, re.M):
            continue
        if vm is None or not EXACT_VERSION.match(vm.group(1)):
            problems.append((i + 1, f"setup: {m.group(0).split('@')[0][6:]} without an exact {key}"))
    return problems


def check_file(path: Path, root: Path = ROOT) -> list[tuple[int, str]]:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    problems: list[tuple[int, str]] = []
    if path.name == "package.json":
        try:
            scripts = json.loads(text).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for name, command in scripts.items():
            for problem in check_command(command):
                problems.append((0, f"{problem} (script {name!r})"))
        # `playwright install --with-deps` installs system packages without versions.
        for name, command in scripts.items():
            if "--with-deps" in command:
                problems.append((0, f"apt: `--with-deps` installs system packages without versions (script {name!r})"))
    else:
        is_dockerfile = path.name.startswith("Dockerfile")
        is_workflow = path.suffix in (".yml", ".yaml")
        args: dict[str, str] = {"__stages__": ""}
        for number, line in logical_lines(text):
            if line.lstrip().startswith("#") and path.suffix not in (".md", ".mdx"):
                continue
            if is_dockerfile:
                am = re.match(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)", line)
                if am:
                    args[am.group(1)] = am.group(2).strip('"')
                sm = re.match(r"^FROM\s.*\sAS\s+(\S+)", line, re.I)
                problem = check_from(line, args)
                if problem:
                    problems.append((number, problem))
                if sm:
                    args["__stages__"] += " " + sm.group(1)
            for problem in check_command(shell_command(line, dockerfile=is_dockerfile, workflow=is_workflow)):
                problems.append((number, problem))
        if is_workflow and ".github/workflows/" in f"/{rel}":
            problems.extend(check_workflow_steps(text))
    return [(n, p) for n, p in problems if (rel, p.split(":", 1)[0]) not in EXEMPT]


def main(root: Path = ROOT) -> int:
    failures = 0
    for path in tracked_files(root):
        for number, problem in check_file(path, root):
            failures += 1
            rel = path.relative_to(root).as_posix()
            where = f"{rel}:{number}" if number else rel
            print(f"{where}: {problem}")
    if failures:
        print(
            f"\n{failures} install(s) without an exact version. Pin them (see the docstring of {Path(__file__).name})."
        )
        return 1
    print("Every install pins an exact version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
