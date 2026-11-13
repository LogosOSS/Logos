#!/usr/bin/env python3
"""Entrypoint of an agent session container.

Sessions run in separate phases of this image, selected by
``LOGOS_SESSION_PHASE`` and run by the runner as separate containers:

* ``ssh-trust`` — isolated, trusted: persist and verify SSH host keys before
  checkout, without access to the workspace or session artefacts.
* ``prepare`` — trusted, with egress and the scoped push token: clone or
  reset the working copy, rebuild its git metadata, and replace the agent's
  home.
* ``agent`` — untrusted, no reusable credentials, and its only network peer
  is the credential-injecting model gateway: the coding agent does the work.
* ``snapshot`` — trusted Git commands capture the candidate tree on its
  original prepared parent; no repository build code executes here.
* ``verify`` — a separate entrypoint, no credentials or network, read-only
  workspace: compile changed Java/Angular services in disposable scratch space.
* ``finalize`` — trusted, with the push token: bring the agent-owned
  checkout and home under trusted metadata again, install the transient
  askpass helper, then commit, push, and open the pull request before
  updating the result file.

Progress goes to stdout, which the runner service collects as the session
transcript; the machine-readable outcome goes to
``$LOGOS_ARTIFACT_DIR/result.json``, which the service reads once the session
has settled.

Everything here runs unprivileged inside a container with no Docker socket, no
workflow-scoped token, and no route to production. That sandbox is what lets
the agent run with permission prompts disabled: there is nothing inside the
agent phase worth protecting from it — and the trusted phases only ever
run fixed harness code, never the agent.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

WORKSPACE = Path("/workspace")
CHECKOUT = WORKSPACE / "repo"
SSH_KNOWN_HOSTS = Path("/logos/ssh_known_hosts")

# The one line the agent writes about what it changed, in its artefact
# directory. The finalizer commits with it. Kept in step with the runner's
# own COMMIT_FILE, which is how it reaches the prompt.
COMMIT_FILE = "commit.txt"
# Where the agent writes what it wants said back on the thread. The runner
# posts it; the name is kept in step with the runner's own REPLY_FILE.
REPLY_FILE = "reply.md"


def log(message: str) -> None:
    print(f"[session] {message}", flush=True)


def fail(message: str) -> None:
    print(f"[session] ERROR: {message}", file=sys.stderr, flush=True)


class Result:
    """Accumulates what the service needs to know about this run."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {
            "branch": os.environ.get("LOGOS_SESSION_BRANCH", ""),
            "pr_url": None,
            "committed": False,
            "files_changed": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "error": None,
        }

    def write(self) -> None:
        """Replace agent-owned results with a fresh regular file, or fail."""
        directory = Path(os.environ.get("LOGOS_ARTIFACT_DIR", "/artifacts"))
        temporary: str | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".result-", suffix=".json", dir=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.data, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            # The directory is the runner's mount. Replace the entry itself:
            # never truncate or follow an agent's symlink, hardlink, or
            # read-only file, and never expose a partially written proof.
            os.replace(temporary, directory / "result.json")
        except OSError as exc:
            raise RuntimeError(f"could not write result file: {exc}") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)


def _redact(cmd: list[str]) -> list[str]:
    """Scrub secrets from a command before it reaches the transcript.

    The transcript is persisted as agent events, rendered in the UI, and
    readable by the agent itself, so a token must never reach it — neither in
    a URL nor in a bare argument.
    """
    token = os.environ.get("GITHUB_TOKEN", "")

    def scrub(part: str) -> str:
        if token and token in part:
            part = part.replace(token, "***")
        # Generic credential-in-URL shape: https://user:password@host
        return re.sub(r"(://[^/:\s]+:)[^@\s]+(@)", r"\1***\2", part)

    return [scrub(c) for c in cmd]


def _without_auto_maintenance(cmd: list[str]) -> list[str]:
    """A git command that starts no background maintenance afterwards.

    A fetch ends by detaching `git maintenance run --auto`, which in a shallow
    checkout can rewrite .git/shallow while the next fetch of the preparation
    reads it; that fetch then dies with "shallow file has changed since we
    read it". The preparation runs its git commands back to back, so none of
    them may leave such a process behind.
    """
    if cmd and cmd[0] == "git":
        return ["git", "-c", "maintenance.auto=false", *cmd[1:]]
    return cmd


def run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess:
    """Run a command, echoing it (secrets redacted) so the transcript shows
    what happened."""
    shown = _redact(cmd)
    if not quiet:
        log(f"$ {' '.join(shlex.quote(c) for c in shown)}")
    # Only trusted harness commands use this wrapper. Each invocation gets
    # empty configuration directories outside the persistent workspace.
    with tempfile.TemporaryDirectory(prefix="logos-command-home-") as home:
        gh_config = Path(home) / "gh"
        xdg_config = Path(home) / "xdg"
        gh_config.mkdir()
        xdg_config.mkdir()
        environment = {
            **os.environ,
            "HOME": home,
            "GH_CONFIG_DIR": str(gh_config),
            "XDG_CONFIG_HOME": str(xdg_config),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        process = subprocess.run(
            _without_auto_maintenance(cmd), cwd=cwd, text=True, capture_output=True, timeout=1800, env=environment
        )
    if process.stdout and not quiet:
        print(process.stdout, end="", flush=True)
    if process.stderr:
        print(process.stderr, end="", file=sys.stderr, flush=True)
    if "failed to add the host to the list of known hosts" in (process.stderr or "").lower():
        raise RuntimeError("SSH host key could not be persisted in the runner's known_hosts file")
    if process.returncode != 0 and any(
        marker in (process.stderr or "").lower()
        for marker in ("remote host identification has changed", "host key verification failed")
    ):
        raise RuntimeError("SSH host key verification failed: the git host key changed or is not trusted")
    if check and process.returncode != 0:
        raise RuntimeError(f"command failed ({process.returncode}): {' '.join(shown)}")
    return process


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _install_git_askpass(token: str) -> None:
    """Teach git to read the push token from the environment, never a URL.

    A credential-in-URL remote would persist the token into .git/config on
    the workspace volume — which outlives the container — and any echo of the
    URL would land it in the transcript. The askpass helper keeps the token
    in the process environment only: git prompts for it on an authenticated
    fetch or push, the helper answers from GITHUB_TOKEN, and nothing on disk
    or in the log ever carries it. Anonymous clones of the public repository
    need no credential at all.
    """
    if not token:
        return
    # A fresh file of our own rather than a fixed name in the shared temp
    # directory, which another process could have created first.
    descriptor, name = tempfile.mkstemp(prefix="logos-git-askpass-", suffix=".sh")
    os.close(descriptor)
    helper = Path(name)
    helper.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  Username*) printf "%s\\n" "x-access-token" ;;\n'
        '  *) printf "%s\\n" "$GITHUB_TOKEN" ;;\n'
        "esac\n"
    )
    helper.chmod(0o700)
    os.environ["GIT_ASKPASS"] = str(helper)
    # Never fall back to a terminal prompt: without the helper, fail fast.
    os.environ["GIT_TERMINAL_PROMPT"] = "0"


# Where the conversation is kept between sessions, on the workspace volume
# and beside the home rather than inside it. The home is wiped at the start
# of every session on purpose; this is the one thing worth carrying across.
MEMORY = WORKSPACE / ".memory"

# What the CLI keeps a conversation in, under the agent's home.
_TRANSCRIPTS = ".claude/projects"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _has_conversation() -> bool:
    """Whether a conversation was restored into this session's home."""
    home = Path(os.environ.get("HOME") or "/workspace/.home")
    projects = home / _TRANSCRIPTS
    return projects.is_dir() and any(projects.rglob("*.jsonl"))


def _save_conversation() -> int:
    """Copy the agent's transcripts out of the home before it is wiped.

    Only the transcripts — `*.jsonl` under the CLI's project directory. Not
    `settings.json`, not hooks, not a `CLAUDE.md`, not a shell profile, not a
    build cache: those are executable configuration a session could have
    written while holding a push token, and the next session must not
    inherit them. A conversation is data the same agent already authored,
    and carrying it is what keeps a pull request from being re-read from
    scratch on every review.
    """
    home = Path(os.environ.get("HOME") or "/workspace/.home")
    source = home / _TRANSCRIPTS
    if not source.is_dir() or source.is_symlink():
        return 0
    kept = 0
    for path in sorted(source.rglob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        target = MEMORY / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        kept += 1
    return kept


def _restore_conversation() -> int:
    """Put the kept transcripts back into a freshly wiped home."""
    if not MEMORY.is_dir() or MEMORY.is_symlink():
        return 0
    home = Path(os.environ.get("HOME") or "/workspace/.home")
    restored = 0
    for path in sorted(MEMORY.rglob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        target = home / _TRANSCRIPTS / path.relative_to(MEMORY)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        restored += 1
    return restored


def _forget_conversation() -> None:
    """Drop what was kept: this workspace is starting on something else."""
    if MEMORY.is_symlink():
        MEMORY.unlink()
    elif MEMORY.is_dir():
        shutil.rmtree(MEMORY)


def _reset_agent_home() -> None:
    """Replace the agent's home with a fresh, empty directory.

    HOME lives on the workspace volume, so everything a previous session
    wrote there survives its container: a ``.gitconfig`` with
    ``core.hooksPath`` or an alias, a ``.claude/settings.json`` whose hooks
    run at the next session's first tool call, a ``CLAUDE.md`` the CLI
    injects as global memory, shell rc files, and tool caches (maven, npm,
    pip) a session could have poisoned to steer the next build. Every one of
    those is agent-writable while the agent holds a push token, and the next
    session is credential-bearing — so none of it is safe to reuse. The
    re-download cost is the price of a clean start; the only state worth
    keeping across sessions is the working copy itself, and even its
    repository metadata is rebuilt, not reused.
    """
    home = Path(os.environ.get("HOME") or "/workspace/.home")
    if home.is_symlink():
        # Unlinked even when the target does not exist: `exists()` follows
        # the link, so a dangling one is invisible to it — but `mkdir`
        # would fail on the surviving path and break every later session.
        home.unlink()
    elif home.is_dir():
        shutil.rmtree(home)
    elif home.exists():
        home.unlink()
    home.mkdir(parents=True, exist_ok=True)
    _seed_hook_store(home)


def _seed_hook_store(home: Path) -> None:
    """Give this session a writable pre-commit store of its own.

    The hook environments are baked into the image, which is exactly what
    lets a session with no network run the linters CI runs. What cannot be
    baked in is the store itself: pre-commit takes a lock file and records
    the config it just ran in a small sqlite database, and the image's root
    filesystem is read-only — so the advertised offline command failed on
    the first thing it tried to write.

    Only the database is copied. Its rows hold the paths of the installed
    environments, which stay where they are: pre-commit reads and executes
    them, and writes to neither.
    """
    store = Path(os.environ.get("PRE_COMMIT_HOME") or (home / "pre-commit"))
    seeded = Path(os.environ.get("PRE_COMMIT_STORE") or "/opt/pre-commit")
    try:
        store.mkdir(parents=True, exist_ok=True)
        database = seeded / "db.db"
        if database.is_file():
            shutil.copy2(database, store / "db.db")
    except OSError as exc:
        # Not fatal: pre-commit falls back to installing the hooks itself,
        # fails without a network, and says so. Better a linter the agent
        # is told is unavailable than a session that never starts.
        print(f"[setup] could not prepare the pre-commit store: {exc}", flush=True)


def _clear_checkout() -> None:
    """Remove whatever a previous session left at the checkout path.

    Same symlink rule as the home: a dangling ``/workspace/repo`` link is
    invisible to ``exists()``, yet ``git clone`` fails on the surviving
    path — left alone, one session could make every later session in the
    workspace fail before the agent even starts.
    """
    if CHECKOUT.is_symlink():
        CHECKOUT.unlink()
    elif CHECKOUT.is_dir():
        # A previous session left a tree without a usable .git (or one it
        # replaced): nothing in it is trusted, so start from nothing.
        shutil.rmtree(CHECKOUT)
    elif CHECKOUT.exists():
        CHECKOUT.unlink()


def _rebuild_git_metadata(repo_url: str) -> None:
    """Replace all agent-owned Git metadata before authenticated operations.

    Hooks, config and object alternates can redirect Git or execute code.
    Even content-addressed object files can have been replaced, so fetch the
    pinned baseline into a new store instead of trusting their local names.
    The working files remain untouched; symlinked metadata is unlinked and
    never traversed. The caller resets the agent's home before this runs.
    """
    git_dir = CHECKOUT / ".git"
    if git_dir.is_symlink() or (git_dir.exists() and not git_dir.is_dir()):
        git_dir.unlink()
    elif git_dir.is_dir():
        shutil.rmtree(git_dir)
    run(["git", "init", "--quiet"], cwd=CHECKOUT, quiet=True)
    run(["git", "remote", "add", "origin", repo_url], cwd=CHECKOUT, quiet=True)


def _remote_default_branch() -> str | None:
    """The branch the remote's HEAD points at, or None when it cannot be read."""
    process = run(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=CHECKOUT, quiet=True, check=False)
    for line in (process.stdout or "").splitlines():
        match = re.match(r"ref: (refs/heads/\S+)\s+HEAD", line)
        if match:
            return match.group(1)[len("refs/heads/") :].strip()
    return None


# The clone and the branch fetches start at this shallow depth, so the merge
# base of two tips is present only when they came apart within so many
# commits. When it is not, _deepen_until_merge_base pulls more in this step
# size, stopping at the ceiling: past it, a missing base is not a shallow
# history to outwait but a checkout that cannot be diffed.
_HISTORY_DEPTH = 50
_DEEPEN_STEP = 250
_MAX_HISTORY_DEPTH = 5000


def _deepen_until_merge_base(default: str, base: str) -> None:
    """Make `git merge-base origin/<default> HEAD` resolvable, or fail.

    A reviewing session is handed a diff against the default branch
    (`origin/<default>...HEAD`), and a triple-dot diff is only a diff from the
    point the two lines last met. A depth-50 history holds that point only
    when the tips came apart within fifty commits; a pull request older than
    that — or based on main before main moved on — cannot be diffed until both
    histories are deep enough to reach the common ancestor. So deepen them in
    steps until the base computes. A base that still cannot be found at the
    ceiling fails the preparation rather than handing the agent a checkout it
    cannot diff: an agent told to review a pull request has to be able to see
    it, and guessing at a diff it cannot see is worse than no session.
    """
    deepened = 0
    while True:
        process = run(
            ["git", "merge-base", f"origin/{default}", "HEAD"],
            cwd=CHECKOUT,
            check=False,
            quiet=True,
        )
        if (process.stdout or "").strip():
            return
        if deepened >= _MAX_HISTORY_DEPTH:
            raise RuntimeError(
                f"no merge base between origin/{default} and HEAD within {_MAX_HISTORY_DEPTH} commits; "
                "the checkout cannot be diffed against the default branch"
            )
        deepened += _DEEPEN_STEP
        # Each line by name and in a fetch of its own: the shallow clone
        # tracks a single branch, so a bare `fetch --deepen` grows only that
        # one, and a single fetch naming both stops deepening the second line
        # once the first has reached the commits they share.
        run(["git", "fetch", f"--deepen={_DEEPEN_STEP}", "origin", base], cwd=CHECKOUT)
        run(
            ["git", "fetch", f"--deepen={_DEEPEN_STEP}", "origin", f"{default}:refs/remotes/origin/{default}"],
            cwd=CHECKOUT,
        )


def prepare_checkout(repo_url: str, base_branch: str, branch: str, token: str) -> None:
    """Get a clean working copy of `base_branch` on a fresh `branch`.

    A session runs unprivileged but with permission prompts disabled and a
    push token in its environment, so the *next* session must not trust
    anything the previous one wrote: the home directory is replaced and the
    repository metadata rebuilt before any git command runs.
    """
    _install_git_askpass(token)
    # The conversation is carried across the wipe when this session
    # continues the same piece of work — an issue that became a pull
    # request, a review on it, the review after that. Re-reading the whole
    # repository on every cycle is what makes a feedback round cost
    # millions of tokens for a change of ten lines.
    continuing = _flag("LOGOS_SESSION_CONTINUE")
    saved = _save_conversation() if continuing else 0
    _reset_agent_home()
    if continuing:
        restored = _restore_conversation()
        log(f"continuing the conversation of this workspace ({saved} kept, {restored} restored)")
    else:
        # A workspace pointed at different work starts with a clean head.
        _forget_conversation()
    if CHECKOUT.is_symlink() or (CHECKOUT.exists() and not CHECKOUT.is_dir()):
        # A session may have replaced the checkout itself with a link: the
        # `.git` check below would follow it into another tree and treat
        # that tree's metadata as this workspace's. The work behind a link
        # is not a working copy of this workspace, so the path is unlinked
        # and the repository comes back as a fresh clone.
        CHECKOUT.unlink()
    # A base that names a ref rather than a branch is a pull request the
    # session may read and may not push to — `refs/pull/<n>/head`, which
    # exists for every pull request including the ones from forks. A
    # question about somebody else's pull request used to be answered from
    # a checkout of the default branch: the agent was asked about a diff it
    # could not see, and could only say so.
    #
    # There is no remote-tracking branch for such a ref, which is the point:
    # the checkout is the code under discussion and there is nothing here to
    # push back to.
    reading = base_branch.startswith("refs/")
    cloned = not (CHECKOUT / ".git").is_dir()
    if cloned:
        log(f"cloning {repo_url} at {base_branch}")
        CHECKOUT.parent.mkdir(parents=True, exist_ok=True)
        _clear_checkout()
        clone = ["git", "clone", "--depth", "50"]
        if not reading:
            # A ref is not a branch name; the clone takes the default head
            # and the fetch below moves it to what was asked for.
            clone += ["--branch", base_branch]
        run([*clone, repo_url, str(CHECKOUT)])
    else:
        log("reusing existing checkout; rebuilding trusted git metadata")
        _rebuild_git_metadata(repo_url)
    if reading or not cloned:
        run(["git", "fetch", "--depth", "50", "origin", base_branch], cwd=CHECKOUT)
        # Discard whatever a previous session left behind: a session starts
        # from the base it was given, never from another session's leftovers.
        run(["git", "reset", "--hard", "FETCH_HEAD" if reading else f"origin/{base_branch}"], cwd=CHECKOUT)
        run(["git", "clean", "-fdx"], cwd=CHECKOUT, check=False)

    # The task a reviewing agent is given tells it to run
    # `git diff origin/<default>...HEAD` — and that ref is not in every
    # checkout this function builds. A clone of a feature branch
    # materialises only that branch, and a fetch of a pull request's ref
    # writes only FETCH_HEAD, so a checkout prepared for either one holds
    # the code under discussion but nothing to diff it against. Fetch the
    # remote's default branch into its remote-tracking ref so the command
    # works however the checkout was built. A failure here does not fail
    # the preparation: the base is already where the session needs it.
    default = os.environ.get("LOGOS_SESSION_REPOSITORY_BRANCH", "").strip() or _remote_default_branch()
    default_available = False
    if default:
        process = run(
            ["git", "fetch", "--depth", "50", "origin", f"{default}:refs/remotes/origin/{default}"],
            cwd=CHECKOUT,
            check=False,
        )
        default_available = process.returncode == 0
        if not default_available:
            log(f"could not fetch the default branch {default!r}; the checkout keeps its base only")

    _configure_git_identity()
    # -B so a retried session reuses its branch name instead of failing.
    run(["git", "checkout", "-B", branch], cwd=CHECKOUT)

    # With the branch checked out, `HEAD` is the tip the session is to be
    # diffed against the default branch from, so this is the same ref the
    # review command uses. Deepen until that diff has a base to start from.
    # Skipped when the default branch could not be fetched at all: the base
    # alone is still a usable working copy, and there is nothing to deepen
    # toward.
    if default_available:
        _deepen_until_merge_base(default, base_branch)


def agent_login() -> str:
    """The GitHub account this session's work belongs to."""
    return (os.environ.get("LOGOS_AGENT_GITHUB_LOGIN") or "LogosOSSAgent").strip()


def _configure_git_identity() -> None:
    """Commit as the agent account, not as an anonymous 'Logos Agent'.

    The account is the same one the token belongs to, so a commit's author
    and the identity that pushed it agree — the history then shows one
    revocable actor rather than a name that resembles the platform.
    """
    login = agent_login()
    run(["git", "config", "user.name", login], cwd=CHECKOUT, quiet=True)
    run(
        ["git", "config", "user.email", f"{login}@users.noreply.github.com"],
        cwd=CHECKOUT,
        quiet=True,
    )


class _NoIdentityRedirects(HTTPRedirectHandler):
    """An authenticated identity request must never leave its fixed endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_token_identity(token: str) -> None:
    """Refuse to push with a token that is not the agent account's.

    The runner checks this at startup, but the container is where the push
    actually happens, and it is the last place the check still helps: a
    token swapped in the environment, or a runner that started before the
    token was rotated, would otherwise commit and open pull requests under
    a human contributor's name.
    """
    expected = agent_login().lower()
    request = Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "logos-agent",
        },
    )
    # Neither gh configuration, environment proxies nor redirects may select
    # the host receiving this credential. urllib verifies HTTPS certificates.
    opener = build_opener(ProxyHandler({}), _NoIdentityRedirects())
    try:
        with opener.open(request, timeout=60) as response:
            identity = json.loads(response.read(65536))
        login = identity["login"].strip()
        if not login:
            raise ValueError("missing login")
    except (URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("could not establish the identity of the push token") from exc
    if login.lower() != expected:
        raise RuntimeError(
            f"the push token authenticates as '{login}', not as the agent account "
            f"'{agent_login()}'; refusing to push agent work under another identity"
        )
    log(f"push token verified as {login}")


def _prepared_checkout_sha() -> str:
    """The immutable baseline supplied by the runner, never the agent's files."""
    sha = os.environ.get("LOGOS_SESSION_CHECKOUT_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise RuntimeError("no trusted prepared checkout SHA; refusing to finalize an unanchored working tree")
    return sha


def _prepared_branch_sha() -> str:
    """The branch lease observed before the untrusted phase started."""
    sha = os.environ.get("LOGOS_SESSION_BRANCH_SHA")
    if sha is None or (sha and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)):
        raise RuntimeError("no trusted prepared branch lease; refusing to replace an unknown remote branch")
    return sha


def _remote_branch_sha(branch: str) -> str:
    """Read an exact branch tip, distinguishing absence from a fetch failure."""
    ref = f"refs/heads/{branch}"
    process = run(["git", "ls-remote", "--exit-code", "origin", ref], cwd=CHECKOUT, check=False, quiet=True)
    if process.returncode == 2:
        return ""
    if process.returncode != 0:
        raise RuntimeError("could not establish the remote session branch tip")
    for line in process.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
            return fields[0]
    raise RuntimeError("the remote session branch did not return a valid commit")


def finalize_checkout(repo_url: str, base_branch: str, branch: str, token: str) -> bool:
    """Restore trusted metadata on the original, runner-recorded checkout SHA.

    The working files belong to the agent. Its home, hooks, configuration,
    refs and object store do not establish either the baseline or the branch
    lease: both come from state the runner captured before the agent started.
    Fetch the exact baseline into fresh metadata, then reset only the index.
    An advancing base branch therefore remains outside this commit's diff,
    and Git can merge independent upstream changes without reverting them.
    """
    _save_conversation()
    _reset_agent_home()
    verify_token_identity(token)
    if CHECKOUT.is_symlink() or (CHECKOUT.exists() and not CHECKOUT.is_dir()):
        log("the checkout is not a directory; there is no work to finalize")
        if CHECKOUT.is_symlink():
            CHECKOUT.unlink()
        return False
    if not (CHECKOUT / ".git").is_dir():
        log("no repository at the checkout; there is no work to finalize")
        return False
    baseline = _prepared_checkout_sha()
    _prepared_branch_sha()
    # Even object files are discarded: the agent can replace their contents
    # or install object alternates. Fetching into a new store establishes the
    # trusted baseline from the remote instead of trusting its local name.
    _rebuild_git_metadata(repo_url)
    _install_git_askpass(token)
    run(["git", "fetch", "--depth", "50", "origin", baseline], cwd=CHECKOUT)
    if _ref_sha("FETCH_HEAD") != baseline:
        raise RuntimeError("the fetched commit does not match the prepared checkout SHA")
    run(["git", "update-ref", f"refs/heads/{branch}", baseline], cwd=CHECKOUT, quiet=True)
    if _remote_branch_sha(branch):
        run(
            ["git", "fetch", "--depth", "50", "origin", f"{branch}:refs/remotes/origin/{branch}"],
            cwd=CHECKOUT,
            quiet=True,
        )
    run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=CHECKOUT, quiet=True)
    run(["git", "reset"], cwd=CHECKOUT, quiet=True)
    _configure_git_identity()
    return True


def build_prompt(task: str) -> str:
    """Wrap the operator's task with the constraints of this environment."""
    images = [path for path in os.environ.get("LOGOS_SESSION_IMAGES", "").split(",") if path.strip()]
    pictures = ""
    if images:
        # An issue whose description is a screenshot is unreadable without
        # this, and the sandbox cannot go and fetch one.
        listed = "\n".join(f"  {path}" for path in images)
        pictures = (
            "\n\nThe request came with images. They have been downloaded for you "
            "and you can open them with the Read tool:\n"
            f"{listed}\n"
            "Look at them before you decide anything — on a visual report they "
            "are usually the whole description.\n"
        )
    # What this container is, as the runner describes it — an operator can
    # adjust that text, and the page shows exactly what was handed over. The
    # text below is the fallback for a session started by an older runner,
    # which is why the test is whether the runner said anything at all: an
    # operator who empties the notes has decided nothing should be said
    # here, and answering that decision with a page of defaults ignores it.
    if "LOGOS_SESSION_ENVIRONMENT_NOTES" in os.environ:
        notes = os.environ["LOGOS_SESSION_ENVIRONMENT_NOTES"].strip()
        return f"{task}{pictures}\n\n{notes}\n" if notes else f"{task}{pictures}\n"
    return (
        f"{task}"
        f"{pictures}\n\n"
        "--- Environment notes ---\n"
        "You are running unattended in an isolated container on a working copy "
        "of this repository. There is no human to ask, so make reasonable "
        "decisions and state your assumptions in the final summary.\n"
        "- Work only inside the current checkout.\n"
        "- Do not run git commit, git push, or gh: the harness commits and "
        "opens the pull request for you after you finish.\n"
        f"- Write the commit subject to $LOGOS_ARTIFACT_DIR/{COMMIT_FILE}: "
        "ONE line, imperative, under 60 characters, saying what the change "
        "does — 'Cancel the queued request when the client goes away', not "
        "'Fixed stuff' and not a description of the task you were given. No "
        "body, no bullet points, no issue numbers.\n"
        "- Run the project's tests for the code you touch and fix failures "
        "before finishing. For Java/Angular run `python3 /usr/local/lib/verify_checkout.py "
        "logos-webservice logos-ui` (omit unchanged services). Offline dependency "
        "caches are installed. The harness repeats these checks before push; "
        "a failed or unavailable check produces only a draft PR and a failed session. "
        "Lint does not replace compilation or type checks. Other services may lack "
        "offline dependencies: report unavailable checks accurately. Read supplied CI "
        "job logs before calling a failure unrelated; request missing logs in your summary.\n"
        "- Lint what you changed: from the top of the checkout, `pre-commit "
        "run --files <the files you changed>`. Same command and same pinned "
        "hooks as CI, installed in this image, no network needed. Which hooks "
        "run is the repository's own convention (its AGENTS.md and pre-commit "
        "config), not yours to re-list. Several of them reformat in place, so "
        "a hook that says it modified your files has already fixed them — run "
        "it once more and it passes. To chase one hook, name it: "
        "`pre-commit run <hook> --files <files>`. A few hooks cannot run in "
        "here without a network; say so if one of those is what failed.\n"
        "- If the task turns out to be impossible or already done, say so "
        "plainly instead of inventing changes.\n"
        f"- Changing nothing is a legitimate outcome, but it is never a silent "
        f"one: if you finish without touching a file, write why into "
        f"$LOGOS_ARTIFACT_DIR/{REPLY_FILE} — what you looked at, what you "
        "would need, what you would change if you were sure. Somebody asked "
        "for this and is waiting to hear something.\n"
        "- Write in English: your final message, your commit subject, your "
        "reply, whatever you put in the pull request.\n"
    )


# What the CLI prints when its connection to the model died mid-answer. The
# runner causes exactly this on purpose: a session paused to give capacity
# back is frozen and taken off the model network, so the response it was
# reading ends under it. Every session that was paused died this way before
# the retry below existed, and every session that was never paused finished.
_INTERRUPTIONS = (
    "connection lost mid-response",
    "connection error",
    "api error: request timed out",
    # What it printed in production the day two sessions were failed after
    # an hour each, because it was in no list.
    "the response stopped arriving",
    "response above may be incomplete",
    "fetch failed",
    "socket hang up",
    "econnreset",
)

# What the runner appends to whenever it freezes this session, in the
# runner's state directory the runner mounts into us read-only. The
# authoritative signal, and the reason the list above is no longer
# load-bearing: matching an upstream tool's prose means a session dies
# quietly the next time somebody rewrites a sentence. The runner froze us;
# it knows, and it says so. The mark is not in the artefact directory:
# that one is ours to write, and a line we could append would reclassify
# our own failures as platform pauses.
INTERRUPTION_FILE = "interruptions"

# How many times a run may be picked up again after an interruption nobody
# claimed. The work itself is in the checkout, so continuing costs a prompt
# and the conversation it resumes; three is few enough that a genuinely
# broken gateway stops being retried.
_MAX_CONTINUATIONS = 3

# And how many times after an interruption the runner *did* claim, by
# freezing this session to give a user their slot. A different number
# because it is a different situation: nothing is broken, the platform is
# busy, and the only question is whether an afternoon of being useful ends
# with the work or without it. Production froze one session twenty-one
# times in eighty minutes — against a bound of three, every session on a
# busy afternoon would be thrown away just short of finishing.
_MAX_PAUSED_CONTINUATIONS = 60


def _agent_command(prompt: str, *, resuming: bool) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        # Safe here precisely because of the sandbox: no socket, no secrets,
        # no production access. Prompting would deadlock an unattended run.
        "--permission-mode",
        "bypassPermissions",
    ]
    if resuming:
        # Same conversation, same working directory: the agent keeps what it
        # has already read and done instead of starting the task again.
        cmd.append("--continue")
    max_turns = os.environ.get("LOGOS_SESSION_MAX_TURNS", "").strip()
    if max_turns.isdigit():
        cmd += ["--max-turns", max_turns]
    return cmd


def _pauses_so_far() -> int:
    """How many times the runner has frozen this session.

    Counted rather than flagged: a run has to know whether it was frozen
    during *itself*, and a flag set by an earlier pause would make every
    later failure look like an interruption.

    The directory is the runner's state, mounted read-only: whatever else
    this container can write, it cannot add a line here, so a count greater
    than the one taken before the run began is a pause that really happened.
    """
    path = Path(os.environ.get("LOGOS_STATE_DIR", "/logos/state")) / INTERRUPTION_FILE
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeDecodeError):
        return 0


def _drive_agent(cmd: list[str]) -> tuple[int, dict[str, object], bool, bool]:
    """Run one invocation.

    Returns its exit code, its usage, whether it was cut off, and whether
    the runner is the one that cut it off — the last two are separate
    because they are allowed different numbers of second chances.
    """
    usage: dict[str, object] = {}
    interrupted = False
    pauses_before = _pauses_so_far()
    process = subprocess.Popen(cmd, cwd=CHECKOUT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if any(marker in line.lower() for marker in _INTERRUPTIONS):
            interrupted = True
        # The stream is newline-delimited JSON; anything that is not valid JSON
        # is the CLI talking to us directly, and is worth showing verbatim.
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line, flush=True)
            continue
        _render_event(event)
        if event.get("type") == "result":
            usage = event
            _account_for(event)
    code = process.wait()
    # The platform froze this session while the run was in flight and cut it
    # off the model network. Whatever the CLI made of that, the answer died
    # because we took the capacity back — which is a thing to continue from,
    # not a thing to fail on.
    frozen = _pauses_so_far() > pauses_before
    return code, usage, interrupted or frozen, frozen


def run_agent(task: str) -> dict[str, object]:
    """Drive the coding agent and return what it reported about the run.

    An interrupted run is picked up rather than failed. The platform takes
    its capacity back by freezing a session and cutting it off the model
    network — deliberately, so a waiting user gets the slot — and the agent
    finds a dead response when it thaws. Treating that as a failed session
    threw away everything it had done: hours of work, uncommitted, in a
    checkout the next session resets.
    """
    prompt = build_prompt(task)
    started = time.monotonic()
    # A conversation restored by the preparation phase is this workspace's
    # own history on this pull request: continue it instead of introducing
    # the repository to a stranger again.
    carry_on = _flag("LOGOS_SESSION_CONTINUE") and _has_conversation()
    if carry_on:
        log("picking up this workspace's earlier conversation")
        # The conversation already holds the change and why it was made, so
        # the task is what is *new*: the review that just came in, the
        # question somebody asked. Saying so beats letting it look like the
        # work is starting over.
        prompt = (
            "This is the same piece of work you have been doing in this "
            "checkout, one round later. Everything you did before is still "
            "here, and so is your reasoning for it — do not start over and "
            "do not re-read what you already know. Here is what came back:"
            f"\n\n{prompt}"
        )
    log("starting agent")
    # Spending is summed across invocations. An interrupted run is still a
    # run — its tokens were spent and its cost incurred — and the result
    # event of the invocation that finished only describes that one.
    spent_in = spent_out = 0
    spent_cost = 0.0
    attempt = 0
    # Counted apart, because they mean different things. One says the
    # platform is busy and took its capacity back; the other says something
    # is wrong that nobody has explained.
    after_a_pause = 0
    unexplained = 0
    while True:
        resuming = attempt > 0 or carry_on
        if attempt > 0:
            log(f"the agent's connection was cut; continuing where it left off (attempt {attempt + 1})")
        # The first run of a continued session carries the new work — the
        # review that just came in — into the conversation that did the
        # earlier rounds. A later run carries only "you were cut off".
        text = CONTINUE_PROMPT if attempt > 0 else prompt
        code, run_usage, interrupted, frozen = _drive_agent(_agent_command(text, resuming=resuming))
        run_in, run_out, run_cost = usage_totals(run_usage)
        if not run_usage:
            # Cut off before it could report: what the assistant events
            # showed is the best account of that invocation there is.
            run_in, run_out = max(run_in, _spent["in"] - spent_in), max(run_out, _spent["out"] - spent_out)
        spent_in += run_in
        spent_out += run_out
        spent_cost += run_cost
        elapsed = time.monotonic() - started
        if code == 0:
            log(f"agent finished in {elapsed:.0f}s with exit code {code}")
            return _totalled(spent_in, spent_out, spent_cost)
        if not interrupted:
            log(f"agent finished in {elapsed:.0f}s with exit code {code}")
            raise RuntimeError(f"agent exited with code {code}")
        if frozen:
            after_a_pause += 1
            spent, ceiling, why = after_a_pause, _MAX_PAUSED_CONTINUATIONS, "pauses"
        else:
            unexplained += 1
            spent, ceiling, why = unexplained, _MAX_CONTINUATIONS, "unexplained interruptions"
        if spent > ceiling:
            # Said rather than left to be inferred from a bare exit code:
            # "this session was interrupted more times than it is allowed to
            # come back from" and "the agent failed" are different endings,
            # and only one of them is about the agent.
            log(f"agent finished in {elapsed:.0f}s after {spent} {why}, which is past the {ceiling} allowed")
            raise RuntimeError(f"the session was cut off {spent} times ({why}) and could not be continued")
        attempt += 1


def _totalled(tokens_in: int, tokens_out: int, cost: float) -> dict[str, object]:
    """One usage record standing for every invocation this session made."""
    return {
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        "total_cost_usd": cost,
    }


# What the agent is told when it comes back from an interruption. Short on
# purpose: the conversation it resumes carries the task, and repeating it
# would invite starting over.
CONTINUE_PROMPT = (
    "Your connection to the model was interrupted — the platform froze this "
    "session to give a waiting user its capacity, and the answer you were "
    "receiving was cut off. Nothing you had already done was lost: the "
    "working copy is exactly as you left it. Carry on from where you were. "
    "If you are unsure how far you got, check `git status` and the files you "
    "were editing before assuming anything."
)

# What has been spent so far, as the transcript goes. The runner reads these
# lines back out of the container's output: it is the only channel out of the
# sandbox, which holds no credential and reaches nothing but the model
# gateway. The totals in the result file at the end are the authority.
_spent = {"in": 0, "out": 0}


def _report_usage(message: dict) -> None:
    """Print what has been spent so far, when it changes.

    Cache reads are deliberately not counted. Every turn re-reads the whole
    conversation out of the cache, so summing that key means counting the
    same tokens once per turn: a session reported seventeen million input
    tokens against no output at all, which is a true sum of a meaningless
    quantity. What is counted is what the model had to take in anew —
    fresh input and cache writes — and what it wrote.

    What it wrote is usually not there yet. The usage on an assistant event
    is the count as the turn *began*, so the output figure is zero all the
    way through a run and only the result event knows the total — which is
    why a session that had written a hundred thousand tokens spent its whole
    life reporting `out=0` on the page. So the number is printed when there
    is one, and left out when there is not: an absent figure reads as
    unknown, and a zero reads as nothing written.
    """
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    read = sum(
        value for key in ("input_tokens", "cache_creation_input_tokens") if isinstance(value := usage.get(key), int)
    )
    written = usage.get("output_tokens")
    before = dict(_spent)
    _spent["in"] += read
    _spent["out"] += written if isinstance(written, int) else 0
    if _spent != before:
        _print_usage()


def _print_usage() -> None:
    """One transcript line for the running total."""
    written = f" out={_spent['out']}" if _spent["out"] else ""
    print(f"[usage] in={_spent['in']}{written}", flush=True)


def _account_for(result: dict) -> None:
    """Take the authoritative totals of a finished invocation.

    The result event is the only place the output count appears, so it is
    folded into the running figures rather than left to the settlement: a
    paused session, a continued one, and the page in between all read the
    transcript, and the transcript should not be the one account that is
    permanently missing half the number.
    """
    tokens_in, tokens_out, _cost = usage_totals(result)
    if tokens_out > _spent["out"]:
        _spent["out"] = tokens_out
    if tokens_in > _spent["in"]:
        _spent["in"] = tokens_in
    _print_usage()


def _render_event(event: dict) -> None:
    """Turn one stream event into a readable transcript line."""
    kind = event.get("type")
    if kind == "assistant":
        message = (event.get("message") or {}) if isinstance(event.get("message"), dict) else {}
        for block in message.get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                print(block["text"].strip(), flush=True)
            elif block.get("type") == "tool_use":
                print(_tool_line(block), flush=True)
        _report_usage(message)
    elif kind == "result":
        subtype = event.get("subtype", "")
        print(f"[result] {subtype}", flush=True)
    elif kind == "system" and event.get("subtype") == "init":
        print(f"[agent] model={event.get('model')}", flush=True)


# How much of a tool call fits on one transcript line. Long enough for a git
# command or a path, short enough that a wall of them is still readable.
_TOOL_DETAIL = 140


def _tool_line(block: dict) -> str:
    """One transcript line for one tool call.

    "[tool] Bash" three times in a row tells a reader nothing — not which
    file was read, not which command ran, not whether the agent is looking
    at the right thing at all. The name is the least interesting part of the
    call; what it was given is the part somebody watching wants.
    """
    name = str(block.get("name") or "tool")
    args = block.get("input")
    detail = _tool_detail(name, args if isinstance(args, dict) else {})
    return f"[tool] {name}: {detail}" if detail else f"[tool] {name}"


def _tool_detail(name: str, args: dict) -> str:
    """The part of a tool call worth showing."""
    if name == "Bash":
        return _one_line(args.get("command"))
    if name in ("Read", "NotebookEdit"):
        where = _short_path(args.get("file_path") or args.get("notebook_path"))
        offset = args.get("offset")
        return f"{where}:{offset}" if where and isinstance(offset, int) else where
    if name in ("Edit", "Write"):
        return _short_path(args.get("file_path"))
    if name == "Grep":
        pattern = _one_line(args.get("pattern"))
        where = _short_path(args.get("path"))
        return f"{pattern} in {where}" if where else pattern
    if name == "Glob":
        return _one_line(args.get("pattern"))
    if name in ("WebFetch", "WebSearch"):
        return _one_line(args.get("url") or args.get("query"))
    if name == "Task":
        return _one_line(args.get("description") or args.get("subagent_type"))
    if name == "TodoWrite":
        todos = args.get("todos")
        if isinstance(todos, list):
            doing = next(
                (
                    str(item.get("content") or "")
                    for item in todos
                    if isinstance(item, dict) and item.get("status") == "in_progress"
                ),
                "",
            )
            return _one_line(f"{len(todos)} steps, on '{doing}'" if doing else f"{len(todos)} steps")
    # An unfamiliar tool: show whatever short string it was given rather
    # than nothing at all.
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return _one_line(value)
    return ""


def _one_line(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[: _TOOL_DETAIL - 1] + "…" if len(text) > _TOOL_DETAIL else text


def _short_path(value: object) -> str:
    """A path as it reads in the repository, not as it sits in the container."""
    text = str(value or "").strip()
    if not text:
        return ""
    prefix = f"{CHECKOUT}/"
    return _one_line(text[len(prefix) :] if text.startswith(prefix) else text)


def usage_totals(usage: dict[str, object]) -> tuple[int, int, float]:
    """Extract token counts and cost from the agent's result event."""
    tokens = usage.get("usage") or {}
    if not isinstance(tokens, dict):
        tokens = {}

    def as_int(*keys: str) -> int:
        for key in keys:
            value = tokens.get(key)
            if isinstance(value, int):
                return value
        return 0

    # Same definition as the running figures the session reports on its
    # transcript, so the number does not change meaning when the session
    # ends: fresh input and cache writes, not the conversation re-read out
    # of the cache on every turn.
    tokens_in = as_int("input_tokens") + as_int("cache_creation_input_tokens")
    tokens_out = as_int("output_tokens")
    # The CLI reports USD; Logos accounts in EUR. Without a live rate the
    # honest thing is to carry the number through unconverted and let the
    # platform's own billing be the authority.
    cost = usage.get("total_cost_usd")
    return tokens_in, tokens_out, float(cost) if isinstance(cost, (int, float)) else 0.0


def changed_files() -> list[str]:
    process = run(["git", "status", "--porcelain"], cwd=CHECKOUT, check=False, quiet=True)
    return [line[3:] for line in process.stdout.splitlines() if line.strip()]


def staged_files() -> list[str]:
    """Every path the next commit adds, changes or removes, exactly as git stores it.

    ``-z`` prints names unquoted and NUL-separated, so unusual characters
    cannot disguise a path, and ``--no-renames`` lists both sides of a move.
    """
    process = run(
        ["git", "diff", "--cached", "--name-only", "-z", "--no-renames"], cwd=CHECKOUT, check=True, quiet=True
    )
    return [path for path in process.stdout.split("\0") if path]


def _ref_sha(*refs: str) -> str | None:
    """The commit a ref names, or None when none of them resolves.

    ``--verify`` makes git fail on a ref that is really a literal string
    rather than a name; ``--quiet`` keeps the failure silent, so a missing
    ref reads as ``None`` instead of an exception.
    """
    for ref in refs:
        process = run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=CHECKOUT, check=False, quiet=True)
        sha = process.stdout.strip()
        if process.returncode == 0 and sha:
            return sha
    return None


# Paths whose contents GitHub executes with the repository's own CI
# permissions and secrets.
_WORKFLOW_PREFIX = ".github/workflows/"


def _refuse_workflow_changes(files: list[str]) -> None:
    """Stop a push that would edit CI, unless the token may do it anyway.

    A push token without `workflow` scope is refused by GitHub the moment a
    commit touches `.github/workflows/`, which is exactly the boundary the
    two-token setup buys: the agent phase may change any part of the
    repository except the part that runs with the repository's secrets. When
    a deployment configures only one token, that token has the scope — and
    then this check is the boundary instead. Failing here is deliberate: the
    session loses its work, which is recoverable, rather than opening a pull
    request whose workflow file runs with CI credentials, which is not.
    """
    if os.environ.get("LOGOS_AGENT_WORKFLOW_CHANGES") == "allow":
        return
    offending = sorted(path for path in files if path.startswith(_WORKFLOW_PREFIX))
    if not offending:
        return
    raise RuntimeError(
        "the session changed CI workflow files ("
        + ", ".join(offending[:5])
        + "), which this runner's push token is not allowed to carry into the "
        "repository. Configure a separate LOGOS_AGENT_SESSION_GITHUB_TOKEN "
        "without 'workflow' scope so GitHub enforces this, or an explicitly "
        "workflow-enabled session token if agent sessions are meant to edit CI."
    )


def _validate_branch_lease(branch: str, tree: str) -> bool:
    """Reject concurrent work before changing either the branch or its PR.

    Return True only when the exact snapshot was already published by this
    session, so restart recovery can keep that commit without replacing it.
    """
    expected = _prepared_branch_sha()
    current = _ref_sha(f"refs/remotes/origin/{branch}") or ""
    if current != expected:
        # A finalizer may have pushed successfully before the runner crashed.
        # Retry only the exact same snapshot on the same original parent;
        # unrelated branch changes must never become a freshly accepted lease.
        remote_tree = _ref_sha(f"{current}^{{tree}}") if current else None
        parents = (
            run(["git", "rev-list", "--parents", "-n", "1", current], cwd=CHECKOUT, quiet=True).stdout.split()
            if current
            else []
        )
        if remote_tree == tree and parents == [current, _prepared_checkout_sha()]:
            log("the exact session snapshot is already pushed; keeping the remote commit")
            return True
        raise RuntimeError(
            "the remote session branch changed after preparation; "
            "refusing an unverified branch tip or overwriting concurrent work"
        )
    return False


def commit_and_push(branch: str, task: str) -> int:
    run(["git", "add", "-A"], cwd=CHECKOUT)
    files = staged_files()
    _refuse_workflow_changes(files)
    tree = run(["git", "write-tree"], cwd=CHECKOUT, quiet=True).stdout.strip()
    if _validate_branch_lease(branch, tree):
        return 0
    if not files:
        log("agent produced no changes; nothing to commit")
        return 0

    log(f"committing {len(files)} changed file(s)")
    run(["git", "commit", "-m", _commit_subject(task)], cwd=CHECKOUT)
    expected = _prepared_branch_sha()
    run(["git", "push", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin", branch], cwd=CHECKOUT)
    return len(files)


# What a subject may be. Fifty is the git convention and seventy-two the
# point at which tools start wrapping; this keeps a summary on one line in
# every viewer.
_SUBJECT_LIMIT = 72


def _commit_subject(task: str) -> str:
    """One line saying what changed.

    The agent writes it, because it is the only one that knows: a subject
    derived from the task describes the *request*, and for a handover that
    reads "Pull request #851 ('…') has been assigned to you" — which is not
    what the commit did. What it writes is trimmed to one line and capitalised,
    so a session cannot fail review on a formatting rule either.
    """
    return (
        _as_subject(_written_subject())
        or _as_subject(os.environ.get("LOGOS_SESSION_SUBJECT", ""))
        or "Update from an agent session"
    )


def _written_subject() -> str:
    """What the agent said about its own change, if it said anything."""
    path = Path(os.environ.get("LOGOS_ARTIFACT_DIR", "/artifacts")) / COMMIT_FILE
    try:
        return path.read_text()
    except OSError:
        return ""


def _as_subject(text: str) -> str:
    """One clean line, or nothing."""
    first_line = next((line for line in (text or "").splitlines() if line.strip()), "")
    cleaned = re.sub(r"\s+", " ", first_line).strip().rstrip(".")
    # The old convention put a component prefix in front; drop one the agent
    # still writes out of habit.
    cleaned = re.sub(r"^`?logos`?\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return ""
    cleaned = cleaned[0].upper() + cleaned[1:]
    if len(cleaned) > _SUBJECT_LIMIT:
        # Cut on a word rather than mid-word, and without a trailing ellipsis:
        # a subject is a sentence, not a preview of one.
        cleaned = cleaned[:_SUBJECT_LIMIT].rsplit(" ", 1)[0].rstrip(",;:-")
    return cleaned


def _closed_issues(closes: str) -> str:
    """The pull request's body, and nothing else.

    The issue the work closes, named the way GitHub reads the reference:
    merging the pull request closes it. The number is the session's own — the
    issue it was assigned — carried in by the runner, not read out of the
    task: the task renders the issue's body and its conversation, and those
    name other issues all the time, a "see #948" pointer among them. A
    pointer is not an authorization to close, so the body names only what the
    runner says the session is the work on. Empty when there is no number,
    which is also the safe default: never close a reference nobody gave. What
    the change does belongs in the commit subject; why it was made belongs to
    whoever picks it up.
    """
    number = closes.strip().lstrip("#")
    if not number.isdigit():
        return ""
    return f"closes #{number}"


def _pull_request_body() -> str:
    """The assigned issue and the runner's verification result."""
    body = _closed_issues(os.environ.get("LOGOS_SESSION_CLOSES", ""))
    verification = os.environ.get("LOGOS_SESSION_VERIFICATION", "")
    if verification:
        output = os.environ.get("LOGOS_SESSION_VERIFICATION_OUTPUT", "").replace("```", "'''")
        label = (
            "Build and test command"
            if os.environ.get("LOGOS_VERIFICATION_MODE") == "repository"
            else "Offline compile/type checks"
        )
        body += f"\n\n## Verification\n\n{label}: **{verification}**."
        if output:
            body += f"\n\n```text\n{output}\n```"
        if verification != "passed":
            body += "\n\nDraft: fix the reported failure or unavailable dependencies before requesting review."
    return body


def open_pull_request(branch: str, base_branch: str, task: str) -> str | None:
    slug = os.environ.get("LOGOS_REPO_SLUG", "").strip()
    if not slug:
        log("no repository slug configured; skipping pull request")
        return None
    title = _commit_subject(task)
    body = _pull_request_body()
    verification = os.environ.get("LOGOS_SESSION_VERIFICATION", "")
    reviewers = _pr_reviewers()
    create_cmd = [
        "gh",
        "pr",
        "create",
        "--repo",
        slug,
        "--base",
        base_branch,
        "--head",
        branch,
        "--title",
        title,
        # Present rather than absent: `gh` prompts for a body it was
        # not given, and a session has no terminal to prompt at.
        # Empty when the session has no issue to close.
        "--body",
        body,
    ]
    if verification != "passed":
        create_cmd.append("--draft")
    for login in reviewers:
        # One flag per login: `gh` accepts repeated `--reviewer`, and a
        # comma-joined value is easy to misread as a single login.
        create_cmd.extend(["--reviewer", login])
    process = run(
        create_cmd,
        cwd=CHECKOUT,
        check=False,
    )
    if process.returncode != 0:
        # An existing pull request for this branch is not a failure: a retried
        # session reuses it. A retry usually changed what the change does —
        # "refuse it" becomes "serve it" — so bring the title and body back in
        # line with the commit subject this run wrote. Without this the pull
        # request keeps the first run's words: the one titled "Refuse X" that,
        # after its second round, is the one that serves X.
        existing = run(
            ["gh", "pr", "view", branch, "--repo", slug, "--json", "number", "url"],
            cwd=CHECKOUT,
            check=False,
            quiet=True,
        )
        try:
            info = json.loads(existing.stdout)
        except ValueError:
            info = {}
        number = info.get("number")
        if not number:
            fail("could not open a pull request")
            return None
        edit_cmd = [
            "gh",
            "pr",
            "edit",
            str(number),
            "--repo",
            slug,
            "--title",
            title,
            "--body",
            body,
        ]
        for login in reviewers:
            # Asking again is a no-op when they are already requested —
            # which is what makes a reused pull request safe to refresh.
            edit_cmd.extend(["--add-reviewer", login])
        refreshed = run(
            edit_cmd,
            cwd=CHECKOUT,
            check=False,
            quiet=True,
        )
        url = str(info.get("url") or "")
        if refreshed.returncode == 0:
            log(f"reused existing pull request {url} and refreshed its title")
        else:
            # The pull request exists and the code is pushed; only its words
            # could not be brought in line. Say so rather than claim a refresh
            # that did not happen, but still hand over the link.
            log(f"reused existing pull request {url}; could not refresh its title")
            if verification:
                raise RuntimeError("could not update the pull request verification report")
        return url or None

    for line in process.stdout.splitlines():
        if line.startswith("https://"):
            return line.strip()
    return None


def _pr_reviewers() -> list[str]:
    """Logins the runner asked to review a freshly opened pull request.

    Empty when the runner named nobody — CODEOWNERS still applies on its
    own. Parsed here rather than hard-coded so a deployment can clear or
    widen the list without rebuilding the session image.
    """
    return [name.strip() for name in os.environ.get("LOGOS_SESSION_PR_REVIEWERS", "").split(",") if name.strip()]


# The agent phase writes result.json; the finalizer, which holds the push
# token, reads it. Bounded so a huge file cannot exhaust the finalizer.
RESULT_FILE_MAX_BYTES = 1024 * 1024


def _read_agent_file(path: Path, limit: int) -> str:
    """Read an agent-written regular file without following a link it planted."""
    # The agent controls the file, not its directory: the artefact mount is
    # the runner's. O_NOFOLLOW refuses a link in its place.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # NOSONAR - fixed path under the artefact mount
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > limit:
            raise OSError(f"larger than {limit} bytes")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise OSError(f"larger than {limit} bytes")
        return data.decode("utf-8")
    finally:
        os.close(fd)


def run_ssh_trust() -> None:
    """Learn or verify host keys without mounting session-controlled files."""
    # Serialise first connections across sessions and runner processes. SSH
    # appends to this same inode; checkout helpers only get a read-only bind.
    with SSH_KNOWN_HOSTS.open("r+") as known_hosts:
        fcntl.flock(known_hosts, fcntl.LOCK_EX)
        scratch = Path("/tmp")  # NOSONAR - the helper container's own private tmpfs
        run(["git", "ls-remote", "--", require_env("LOGOS_REPO_URL"), "HEAD"], cwd=scratch, quiet=True)


def _checkout_comparison(base_branch: str, branch: str, branch_sha: str) -> tuple[str, str]:
    """Pin the complete proposed change's merge base before the agent starts.

    A continued session begins at the existing PR tip. Comparing only with
    that tip would skip services changed by earlier sessions. The PR target
    is authoritative when one exists. Otherwise the runner can retain the
    original target across a successful push whose PR creation failed.
    """
    if not branch_sha:
        return require_checkout_ref("HEAD"), base_branch
    target = ""
    slug = os.environ.get("LOGOS_REPO_SLUG", "").strip()
    if slug:
        response = run(
            ["gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json", "baseRefName"],
            cwd=CHECKOUT,
            quiet=True,
        )
        pulls = json.loads(response.stdout)
        if not isinstance(pulls, list) or any(
            not isinstance(pr, dict) or not isinstance(pr.get("baseRefName"), str) or not pr["baseRefName"]
            for pr in pulls
        ):
            raise RuntimeError("could not establish the existing pull request's target branch")
        targets = {pr["baseRefName"] for pr in pulls}
        if len(targets) > 1:
            raise RuntimeError("the session branch has pull requests with different target branches")
        target = next(iter(targets), "")
    target = (
        target
        or os.environ.get("LOGOS_SESSION_PR_BASE_BRANCH", "").strip()
        or os.environ.get("LOGOS_SESSION_REPOSITORY_BRANCH", "").strip()
        or _remote_default_branch()
    )
    if not target:
        raise RuntimeError("could not establish the comparison branch for the existing session branch")
    run(["git", "check-ref-format", f"refs/heads/{target}"], cwd=CHECKOUT, quiet=True)
    run(["git", "fetch", "--depth", "50", "origin", f"{target}:refs/remotes/origin/{target}"], cwd=CHECKOUT)
    _deepen_until_merge_base(target, base_branch)
    comparison = run(["git", "merge-base", f"origin/{target}", "HEAD"], cwd=CHECKOUT, quiet=True).stdout.strip()
    return comparison, target


def require_checkout_ref(ref: str) -> str:
    """Resolve a commit after trusted preparation, or reject incomplete proof."""
    sha = _ref_sha(ref)
    if not sha or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise RuntimeError("the prepared checkout did not provide a valid comparison commit")
    return sha


def run_prepare(result: Result) -> None:
    """Trusted phase one: bring the working copy to a trusted state.

    Runs in a helper container with egress and the scoped push token —
    the agent phase has neither, so whatever the agent later finds under
    /workspace was created here, by fixed harness code. The commit it
    checked out is reported, so a review can be anchored to the code that
    was actually read.
    """
    repo_url = require_env("LOGOS_REPO_URL")
    base_branch = os.environ.get("LOGOS_SESSION_BASE_BRANCH", "main")
    branch = require_env("LOGOS_SESSION_BRANCH")
    token = os.environ.get("GITHUB_TOKEN", "")
    prepare_checkout(repo_url, base_branch, branch, token)
    result.data["checkout_sha"] = _ref_sha("HEAD")
    result.data["checkout_branch_sha"] = _remote_branch_sha(branch)
    if result.data["checkout_branch_sha"] and result.data["checkout_branch_sha"] != result.data["checkout_sha"]:
        raise RuntimeError("the existing session branch differs from the prepared checkout; prepare its current tip")
    comparison, pr_base_branch = _checkout_comparison(base_branch, branch, result.data["checkout_branch_sha"])
    result.data["checkout_comparison_sha"] = comparison
    result.data["checkout_pr_base_branch"] = pr_base_branch


def run_agent_phase(result: Result) -> None:
    """Untrusted phase two: the coding agent works on the checkout.

    This container holds no reusable credential: model traffic goes only to
    the credential-injecting gateway (the token in the environment is a
    placeholder the gateway replaces), and the GitHub operations belong to
    the finalizer. So even an agent that is steered into exfiltrating its
    environment can exfiltrate nothing of value.
    """
    task = require_env("LOGOS_SESSION_TASK")
    require_env("LOGOS_SESSION_BRANCH")
    if not os.environ.get("ANTHROPIC_BASE_URL"):
        raise RuntimeError("no model endpoint provided; the agent has no model to call")

    usage = run_agent(task)

    tokens_in, tokens_out, cost = usage_totals(usage)
    result.data.update(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
    # Screenshots are the runner's job, taken after settlement: a
    # session's own view of the dev environment is stale the moment its
    # deploy is queued, and the runner is the one that knows when the
    # deploy has landed.
    log("agent finished; handing the checkout to the finalizer")


def _restore_agent_result(result: Result) -> None:
    path = Path(os.environ.get("LOGOS_ARTIFACT_DIR", "/artifacts")) / "result.json"
    try:
        data = json.loads(_read_agent_file(path, RESULT_FILE_MAX_BYTES))
        # Only usage is agent-authored. Publishing outcomes and verification
        # must come from this trusted run, never a planted result.json.
        for key in ("tokens_in", "tokens_out", "cost_usd"):
            if isinstance(data.get(key), (int, float)):
                result.data[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"ignoring unreadable result file from the agent phase: {exc}")


def run_snapshot(result: Result) -> None:
    """Rebuild trusted Git and describe the exact candidate without executing repository code."""
    _restore_agent_result(result)
    if not finalize_checkout(
        require_env("LOGOS_REPO_URL"),
        os.environ.get("LOGOS_SESSION_BASE_BRANCH", "main"),
        require_env("LOGOS_SESSION_BRANCH"),
        os.environ.get("GITHUB_TOKEN", ""),
    ):
        raise RuntimeError("no checkout available to verify")
    comparison = require_env("LOGOS_SESSION_COMPARISON_SHA")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", comparison):
        raise RuntimeError("invalid trusted comparison commit")
    run(["git", "fetch", "--depth", "1", "origin", comparison], cwd=CHECKOUT)
    if _ref_sha("FETCH_HEAD") != comparison:
        raise RuntimeError("the fetched comparison does not match the prepared comparison SHA")
    run(["git", "add", "-A"], cwd=CHECKOUT)
    process = run(
        ["git", "diff", "--cached", "--name-only", "-z", "--no-renames", comparison, "--"],
        cwd=CHECKOUT,
        quiet=True,
    )
    result.data["candidate_paths"] = [path for path in process.stdout.split("\0") if path]
    result.data["candidate_tree"] = run(["git", "write-tree"], cwd=CHECKOUT, quiet=True).stdout.strip()


def _draft_existing_pull_request(branch: str) -> None:
    """Make an existing PR a draft before an unverified commit can reach it."""
    slug = os.environ.get("LOGOS_REPO_SLUG", "").strip()
    if not slug:
        raise RuntimeError("cannot publish failed verification without a repository for a draft PR")
    response = run(
        ["gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json", "number,isDraft"],
        cwd=CHECKOUT,
        quiet=True,
    )
    for pr in json.loads(response.stdout):
        if not pr.get("isDraft"):
            run(["gh", "pr", "ready", str(pr["number"]), "--repo", slug, "--undo"], cwd=CHECKOUT)
        run(
            ["gh", "pr", "edit", str(pr["number"]), "--repo", slug, "--body", _pull_request_body()],
            cwd=CHECKOUT,
        )


def run_finalize(result: Result) -> None:
    """Trusted phase three: the authenticated GitHub operations.

    The agent phase never held a GitHub token; this container gets the
    scoped one and egress, and is the only phase allowed to talk to GitHub
    with it. Before the token is introduced, the checkout and home are
    brought under trusted metadata again — the agent owned both while it
    ran — and the askpass helper is installed only after that rebuild, so
    ``git push`` authenticates with a credential nothing planted can
    intercept. It adds its outcome to the result file the agent phase
    wrote (tokens, cost) rather than replacing it.
    """
    branch = require_env("LOGOS_SESSION_BRANCH")
    task = require_env("LOGOS_SESSION_TASK")
    base_branch = os.environ.get("LOGOS_SESSION_BASE_BRANCH", "main")
    repo_url = require_env("LOGOS_REPO_URL")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log("no GitHub token provided; leaving changes uncommitted in the workspace")
        return
    _restore_agent_result(result)
    verification = require_env("LOGOS_SESSION_VERIFICATION")
    if verification not in {"passed", "failed"}:
        raise RuntimeError("invalid verification outcome")
    expected_tree = require_env("LOGOS_SESSION_VERIFIED_TREE")
    pr_base_branch = require_env("LOGOS_SESSION_PR_BASE_BRANCH")
    if not finalize_checkout(repo_url, base_branch, branch, token):
        raise RuntimeError("no verified checkout available to publish")
    run(["git", "add", "-A"], cwd=CHECKOUT)
    actual_tree = run(["git", "write-tree"], cwd=CHECKOUT, quiet=True).stdout.strip()
    if actual_tree != expected_tree:
        raise RuntimeError("checkout changed after verification; refusing to push")
    _validate_branch_lease(branch, actual_tree)
    if verification == "failed":
        _draft_existing_pull_request(branch)
    count = commit_and_push(branch, task)
    result.data["files_changed"] = count
    result.data["committed"] = count > 0
    result.data["verification"] = verification
    result.data["pr_base_branch"] = pr_base_branch
    result.data["published_tree"] = actual_tree
    # Preserve the trusted publication outcome before any fallible PR API
    # operation. A retry must prepare this published branch tip while still
    # targeting the original PR base, even if opening that PR failed.
    # A branch that has never been pushed has no publication proof.
    result.data["pushed_sha"] = _ref_sha("HEAD") if count else _ref_sha(f"refs/remotes/origin/{branch}")
    result.write()
    if result.data["pushed_sha"] and (os.environ.get("LOGOS_SESSION_OPEN_PR") == "1" or verification == "failed"):
        result.data["pr_url"] = open_pull_request(branch, pr_base_branch, task)
        if not result.data["pr_url"]:
            raise RuntimeError("could not publish the pull request and its verification report")


def main() -> int:
    """Run whichever phase of the session this container was asked for.

    The runner runs SSH trust, preparation, agent work, and finalization in
    separate containers: trusted phases carry credentials and egress, the
    untrusted agent phase carries neither.
    """
    phase = os.environ.get("LOGOS_SESSION_PHASE", "agent").strip().lower()
    result = Result()
    exit_code = 0
    try:
        if phase == "ssh-trust":
            run_ssh_trust()
        elif phase == "prepare":
            run_prepare(result)
        elif phase == "snapshot":
            run_snapshot(result)
        elif phase == "finalize":
            run_finalize(result)
        else:
            run_agent_phase(result)
    except Exception as exc:
        fail(str(exc))
        result.data["error"] = str(exc)
        exit_code = 1
    try:
        result.write()
    except Exception as exc:
        fail(f"could not persist the phase result: {exc}")
        return 1
    if exit_code == 0:
        log("phase complete")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
