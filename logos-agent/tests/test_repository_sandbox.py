"""Working on a linked repository: disclosure, GitHub requests, egress, cache and forks."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import conventions, db, disclosure, docker_engine, egress_proxy, github, github_broker, main, sessions
from app.auth import Principal
from app.schemas import SessionCreate
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workspace"))

import run_session  # noqa: E402


def _principal() -> Principal:
    return Principal(subject="op", username="operator", roles=frozenset({"logos_admin"}))


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


# --- disclosure -------------------------------------------------------------


class TestDisclosure:
    def test_the_footer_names_the_account_the_model_and_the_session(self):
        line = disclosure.footer(model="qwen3-coder", session_id=42)

        assert "qwen3-coder" in line
        assert "Logos agent session 42" in line
        assert disclosure.settings.github_login in line

    def test_a_model_name_cannot_break_out_of_the_footer(self):
        line = disclosure.footer(model="x`<script>\n", session_id=1)

        assert "`x<script" not in line
        assert "\n" not in line

    def test_an_unknown_model_is_said_to_be_unknown(self):
        assert "unknown model" in disclosure.footer(model=None, session_id=None)

    def test_publishing_without_a_footer_is_refused(self):
        with pytest.raises(ValueError):
            disclosure.disclose("text", "")

    async def test_a_review_names_the_model_on_every_inline_comment(self, monkeypatch):
        sent: list = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers=None, json=None):
                sent.append(json)
                return FakeResponse(200, {"html_url": "https://github.com/x/y/pull/1#r"})

        monkeypatch.setattr(github.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(github, "settings", replace(github.settings, github_token="tok"))

        await github.create_pull_review(
            1, "summary", [{"path": "a.py", "line": 3, "body": "nit"}], commit_id="abc", footer="<sub>m</sub>"
        )

        assert sent[0]["body"].endswith("<sub>m</sub>")
        assert sent[0]["comments"][0]["body"] == "nit\n\n<sub>m</sub>"

    def test_the_pull_request_description_ends_with_the_disclosure(self, monkeypatch):
        monkeypatch.setenv("LOGOS_SESSION_CLOSES", "12")
        monkeypatch.delenv("LOGOS_SESSION_VERIFICATION", raising=False)
        monkeypatch.setenv("LOGOS_SESSION_DISCLOSURE", "<sub>model m</sub>")

        assert run_session._pull_request_body() == "closes #12\n\n<sub>model m</sub>"

    def test_the_launch_records_the_model_the_session_runs_with(self):
        session = {"id": 5, "model": "the-model"}

        assert "the-model" in sessions._footer(5, session)


# --- GitHub requests ----------------------------------------------------------


def _request(artifacts: Path, name: str, payload: dict) -> None:
    directory = artifacts / github_broker.REQUEST_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(payload))


def _answer(state: Path, name: str) -> dict:
    return json.loads((state / github_broker.RESPONSE_DIR / f"{name}.json").read_text())


class TestGitHubRequests:
    @pytest.fixture
    def dirs(self, tmp_path):
        artifacts, state = tmp_path / "artifacts", tmp_path / "state"
        artifacts.mkdir()
        state.mkdir()
        return artifacts, state

    @staticmethod
    async def _process(artifacts, state, *, may_comment=True):
        lines: list[str] = []

        async def record(line):
            lines.append(line)

        await github_broker.process(
            {"id": 9, "model": "m"}, artifacts=artifacts, state=state, may_comment=may_comment, record=record
        )
        return lines

    async def test_a_comment_is_posted_with_mentions_disabled_and_the_model_named(self, monkeypatch, dirs):
        artifacts, state = dirs
        posted: list = []

        async def post(number, body, *, footer):
            posted.append((number, body, footer))
            return "https://github.com/o/r/issues/5#c"

        monkeypatch.setattr(github, "post_issue_comment", post)
        _request(artifacts, "a", {"action": "comment", "number": 5, "body": "Claiming, cc @someone"})

        lines = await self._process(artifacts, state)

        assert posted[0][0] == 5
        assert "@​someone" in posted[0][1]
        assert "Logos agent session 9" in posted[0][2]
        assert _answer(state, "a") == {"ok": True, "action": "comment", "url": "https://github.com/o/r/issues/5#c"}
        assert lines and "commented on #5" in lines[0]

    async def test_a_comment_is_refused_where_comments_are_not_allowed(self, monkeypatch, dirs):
        artifacts, state = dirs

        async def post(*_args, **_kwargs):
            raise AssertionError("must not post")

        monkeypatch.setattr(github, "post_issue_comment", post)
        _request(artifacts, "a", {"action": "comment", "number": 5, "body": "Claiming"})

        await self._process(artifacts, state, may_comment=False)

        assert _answer(state, "a")["ok"] is False

    async def test_comments_are_bounded_per_session(self, monkeypatch, dirs):
        artifacts, state = dirs
        responses = state / github_broker.RESPONSE_DIR
        responses.mkdir()
        for index in range(github_broker.MAX_COMMENTS_PER_SESSION):
            (responses / f"old{index}.json").write_text(json.dumps({"ok": True, "action": "comment"}))

        async def post(*_args, **_kwargs):
            raise AssertionError("must not post")

        monkeypatch.setattr(github, "post_issue_comment", post)
        _request(artifacts, "a", {"action": "comment", "number": 5, "body": "one more"})

        await self._process(artifacts, state)

        assert "used all its comments" in _answer(state, "a")["error"]

    async def test_an_answered_request_is_not_carried_out_twice(self, monkeypatch, dirs):
        artifacts, state = dirs
        calls: list = []

        async def post(number, body, *, footer):
            calls.append(number)
            return "u"

        monkeypatch.setattr(github, "post_issue_comment", post)
        _request(artifacts, "a", {"action": "comment", "number": 5, "body": "Claiming"})

        await self._process(artifacts, state)
        await self._process(artifacts, state)

        assert calls == [5]

    async def test_an_interrupted_comment_is_reported_rather_than_repeated(self, monkeypatch, dirs):
        artifacts, state = dirs
        (state / github_broker.RESPONSE_DIR).mkdir()
        (state / github_broker.RESPONSE_DIR / "a.json.pending").write_text("{}")

        async def post(*_args, **_kwargs):
            raise AssertionError("must not post again")

        monkeypatch.setattr(github, "post_issue_comment", post)
        _request(artifacts, "a", {"action": "comment", "number": 5, "body": "Claiming"})

        await self._process(artifacts, state)

        assert "interrupted" in _answer(state, "a")["error"]

    async def test_an_invalid_request_is_answered_with_the_reason(self, dirs):
        artifacts, state = dirs
        _request(artifacts, "a", {"action": "close", "number": 5})
        _request(artifacts, "b", {"action": "issue", "number": 5, "repository": "other/repo"})

        await self._process(artifacts, state)

        assert _answer(state, "a")["action"] == "invalid"
        assert _answer(state, "b")["action"] == "invalid"

    async def test_a_linked_request_directory_is_not_followed(self, tmp_path):
        artifacts, state = tmp_path / "artifacts", tmp_path / "state"
        artifacts.mkdir()
        state.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "a.json").write_text(json.dumps({"action": "issue", "number": 1}))
        os.symlink(elsewhere, artifacts / github_broker.REQUEST_DIR)

        await self._process(artifacts, state)

        assert not (state / github_broker.RESPONSE_DIR / "a.json").exists()

    async def test_issues_are_listed_with_validated_filters(self, monkeypatch, dirs):
        artifacts, state = dirs
        seen: list = []

        async def list_issues(**kwargs):
            seen.append(kwargs)
            return [{"number": 1}]

        monkeypatch.setattr(github, "list_issues", list_issues)
        _request(artifacts, "a", {"action": "issues", "labels": ["proof"], "assignee": "none"})
        _request(artifacts, "b", {"action": "issues", "labels": ["a,b"]})

        await self._process(artifacts, state)

        assert seen == [{"labels": ["proof"], "state": "open", "assignee": "none", "page": 1}]
        assert _answer(state, "a")["issues"] == [{"number": 1}]
        assert _answer(state, "b")["ok"] is False

    async def test_only_linked_repository_sessions_are_served(self, monkeypatch):
        served: list = []

        async def process(session, **_kwargs):
            served.append(session["id"])

        async def permissions(_link):
            return {"agent_egress_hosts": [], "agent_may_comment": True}

        monkeypatch.setattr(sessions.github_broker, "process", process)
        monkeypatch.setattr(sessions.db, "sandbox_permissions", permissions)

        await sessions.SessionManager().serve_github_requests(
            [
                {"id": 1, "repo_slug": None, "team_repository_id": None},
                {"id": 2, "repo_slug": "o/r", "team_repository_id": 4},
            ]
        )

        assert served == [2]


class TestTheCommandInTheSession:
    def test_it_writes_a_request_and_prints_the_answer(self, tmp_path, monkeypatch, capsys):
        import logos_github

        artifacts, state = tmp_path / "artifacts", tmp_path / "state"
        monkeypatch.setenv("LOGOS_ARTIFACT_DIR", str(artifacts))
        monkeypatch.setenv("LOGOS_STATE_DIR", str(state))
        monkeypatch.setattr(logos_github.uuid, "uuid4", lambda: SimpleNamespace(hex="abc"))
        (state / "github").mkdir(parents=True)
        (state / "github" / "abc.json").write_text(json.dumps({"ok": True, "action": "comment", "url": "u"}))

        code = logos_github.main(["comment", "5", "Claiming"])

        assert code == 0
        assert json.loads((artifacts / "github-requests" / "abc.json").read_text()) == {
            "action": "comment",
            "number": 5,
            "body": "Claiming",
        }
        assert '"url": "u"' in capsys.readouterr().out


# --- egress proxy -------------------------------------------------------------


class TestEgressProxy:
    def test_hosts_match_exactly_or_by_subdomain_wildcard(self):
        allowed = ["github.com", "*.githubusercontent.com"]

        assert egress_proxy.host_allowed("github.com", allowed)
        assert egress_proxy.host_allowed("objects.githubusercontent.com", allowed)
        assert not egress_proxy.host_allowed("githubusercontent.com", allowed)
        assert not egress_proxy.host_allowed("api.github.com", allowed)
        assert not egress_proxy.host_allowed("evilgithub.com", allowed)

    async def test_names_resolving_to_private_addresses_are_refused(self):
        assert await egress_proxy.public_addresses("localhost", 443) == []

    @staticmethod
    async def _ask(proxy, request: bytes) -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(request)
        reader.feed_eof()
        written = bytearray()

        class Writer:
            def write(self, data):
                written.extend(data)

            async def drain(self):
                return None

            def close(self):
                return None

            def can_write_eof(self):
                return False

        await proxy.handle(reader, Writer())
        return bytes(written)

    @staticmethod
    def _auth(user="session-7", password="tok") -> str:
        return base64.b64encode(f"{user}:{password}".encode()).decode()

    @pytest.fixture
    def proxy(self, monkeypatch):
        async def grant(session_id):
            if session_id != 7:
                return None
            return {"status": "running", "token_hash": egress_proxy.token_hash("tok"), "hosts": ["github.com"]}

        monkeypatch.setattr(egress_proxy.db, "egress_grant", grant)
        return egress_proxy.EgressProxy()

    async def test_plain_http_is_refused(self, proxy):
        answer = await self._ask(proxy, b"GET http://github.com/ HTTP/1.1\r\nHost: github.com\r\n\r\n")

        assert answer.startswith(b"HTTP/1.1 405")

    async def test_other_ports_are_refused(self, proxy):
        answer = await self._ask(
            proxy, f"CONNECT github.com:22 HTTP/1.1\r\nProxy-Authorization: Basic {self._auth()}\r\n\r\n".encode()
        )

        assert answer.startswith(b"HTTP/1.1 403")

    async def test_a_wrong_token_is_refused(self, proxy):
        wrong = self._auth(password="x")
        answer = await self._ask(
            proxy, f"CONNECT github.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {wrong}\r\n\r\n".encode()
        )

        assert answer.startswith(b"HTTP/1.1 407")

    async def test_a_session_that_is_not_running_is_refused(self, proxy, monkeypatch):
        async def grant(_session_id):
            return {"status": "succeeded", "token_hash": egress_proxy.token_hash("tok"), "hosts": ["github.com"]}

        monkeypatch.setattr(egress_proxy.db, "egress_grant", grant)
        answer = await self._ask(
            proxy, f"CONNECT github.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {self._auth()}\r\n\r\n".encode()
        )

        assert answer.startswith(b"HTTP/1.1 407")

    async def test_a_host_off_the_list_is_refused(self, proxy):
        answer = await self._ask(
            proxy, f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {self._auth()}\r\n\r\n".encode()
        )

        assert answer.startswith(b"HTTP/1.1 403")

    async def test_an_allowed_host_is_connected_to_the_checked_address(self, proxy, monkeypatch):
        tried: list = []

        async def addresses(host, port):
            return ["140.82.121.4"]

        async def connect(candidates):
            tried.extend(candidates)
            upstream = asyncio.StreamReader()
            upstream.feed_eof()

            class Sink:
                def write(self, data):
                    pass

                async def drain(self):
                    pass

                def close(self):
                    pass

                def can_write_eof(self):
                    return False

            return upstream, Sink()

        monkeypatch.setattr(egress_proxy, "public_addresses", addresses)
        monkeypatch.setattr(proxy, "_connect", connect)
        answer = await self._ask(
            proxy, f"CONNECT github.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {self._auth()}\r\n\r\n".encode()
        )

        assert answer.startswith(b"HTTP/1.1 200")
        assert tried == ["140.82.121.4"]


class TestSessionEnvironment:
    def test_a_linked_session_reaches_the_internet_only_through_its_proxy(self, monkeypatch):
        monkeypatch.setattr(
            sessions,
            "settings",
            replace(
                sessions.settings,
                session_model_url="http://logos-agent-gateway",
                egress_proxy_url="http://logos-agent-egress-proxy:3128",
            ),
        )
        monkeypatch.setattr(sessions.model_policy, "current", lambda: SimpleNamespace(resolve=lambda m: "m"))
        env = sessions.SessionManager()._session_env({"id": 3, "task": "t"}, "b", egress_token="a/b+c", sandboxed=True)

        assert env["HTTPS_PROXY"] == "http://session-3:a%2Fb%2Bc@logos-agent-egress-proxy:3128"
        assert env["https_proxy"] == env["HTTPS_PROXY"]
        assert "logos-agent-gateway" in env["NO_PROXY"].split(",")
        assert env["LOGOS_CACHE_DIR"] == "/cache"

    def test_a_platform_session_has_no_proxy_and_no_cache(self, monkeypatch):
        monkeypatch.setattr(sessions.model_policy, "current", lambda: SimpleNamespace(resolve=lambda m: "m"))
        env = sessions.SessionManager()._session_env({"id": 3, "task": "t"}, "b")

        assert "HTTPS_PROXY" not in env
        assert "LOGOS_CACHE_DIR" not in env

    def test_the_notes_describe_what_the_session_may_do(self):
        notes = conventions.repository_environment_notes(egress_hosts=["github.com"], may_comment=True, sandboxed=True)

        assert "github.com" in notes
        assert "logos-github comment" in notes
        assert "/cache" in notes
        assert "closes.txt" in notes

    def test_the_notes_say_when_nothing_is_allowed(self):
        notes = conventions.repository_environment_notes(egress_hosts=[], may_comment=False, sandboxed=True)

        assert "Internet: none" in notes
        assert "Commenting is not allowed" in notes


class TestWorkspaceCache:
    @staticmethod
    async def _create(monkeypatch, phase):
        sent: list = []

        async def fake_request(method, path, **kwargs):
            sent.append(kwargs.get("json"))
            return FakeResponse(201, {"Id": "c1"})

        monkeypatch.setattr(docker_engine, "_request", fake_request)
        await docker_engine.create_session_container(
            name="n",
            image="i",
            env={"LOGOS_SESSION_PHASE": phase},
            workspace_volume="ws",
            artifact_host_path="/a",
            session_id=1,
            cache_volume="logos_agent_cache_ws",
        )
        return sent[0]["HostConfig"]["Binds"]

    async def test_the_agent_writes_the_cache(self, monkeypatch):
        assert "logos_agent_cache_ws:/cache" in await self._create(monkeypatch, "agent")

    async def test_verification_reads_it(self, monkeypatch):
        assert "logos_agent_cache_ws:/cache:ro" in await self._create(monkeypatch, "verify")

    async def test_a_credential_bearing_helper_never_mounts_it(self, monkeypatch):
        with pytest.raises(ValueError):
            await self._create(monkeypatch, "finalize")


# --- manual sessions on a linked repository -----------------------------------


LINK = {
    "id": 4,
    "team_id": 2,
    "repo_url": "https://github.com/o/r",
    "repo_slug": "o/r",
    "branch": "develop",
    "agent_instructions": "Never change a statement.",
    "verification_command": "make test",
    "agent_egress_hosts": ["github.com"],
    "agent_may_comment": True,
}


class TestManualLinkedSessions:
    @pytest.fixture
    def created(self, monkeypatch):
        rows: list = []
        repointed: list = []

        async def get_workspace(_workspace_id):
            return {"id": 3, "name": "ws", "base_branch": "main"}

        async def refresh():
            return SimpleNamespace(allows=lambda _model: True)

        async def coding_repository(link_id):
            return dict(LINK) if link_id == 4 else None

        async def workspace_target(_workspace_id):
            return {"repo_slug": None, "busy": False}

        async def set_base(workspace_id, branch):
            repointed.append((workspace_id, branch))

        async def create_session(**kwargs):
            rows.append(kwargs)
            return 21

        async def get_session(session_id):
            from tests.test_app import TestRunningWorkAgain

            return {**TestRunningWorkAgain.ROW, "id": session_id, "status": "queued"}

        monkeypatch.setattr(main, "settings", replace(main.settings, agent_api_key="a-key"))
        monkeypatch.setattr(db, "get_workspace", get_workspace)
        monkeypatch.setattr(main.model_policy, "refresh", refresh)
        monkeypatch.setattr(db, "coding_repository", coding_repository)
        monkeypatch.setattr(db, "workspace_target", workspace_target)
        monkeypatch.setattr(db, "set_workspace_base_branch", set_base)
        monkeypatch.setattr(db, "create_session", create_session)
        monkeypatch.setattr(db, "get_session", get_session)
        monkeypatch.setattr(main, "_summary_fields", lambda row: dict(row))
        monkeypatch.setattr(main.manager, "request_scheduler_pass", lambda: None)
        return rows, repointed

    async def test_the_session_targets_the_repository_with_its_instructions(self, created):
        rows, repointed = created

        await main.create_session(
            SessionCreate(workspace_id=3, task="Prove the lemma", team_repository_id=4), principal=_principal()
        )

        row = rows[0]
        assert row["repo_slug"] == "o/r"
        assert row["team_repository_id"] == 4
        assert row["verification_command"] == "make test"
        assert row["repo_base_branch"] == "develop"
        assert "Never change a statement." in row["task"]
        assert row["task"].startswith("Prove the lemma")
        assert repointed == [(3, "develop")]

    async def test_deploys_and_screenshots_are_refused(self, created):
        with pytest.raises(HTTPException) as refused:
            await main.create_session(
                SessionCreate(workspace_id=3, task="Prove the lemma", team_repository_id=4, deploy_to_dev=True),
                principal=_principal(),
            )

        assert refused.value.status_code == 400

    async def test_a_repository_without_the_agent_is_not_found(self, created):
        with pytest.raises(HTTPException) as refused:
            await main.create_session(
                SessionCreate(workspace_id=3, task="Prove the lemma", team_repository_id=99), principal=_principal()
            )

        assert refused.value.status_code == 404

    async def test_a_busy_workspace_of_another_repository_is_refused(self, created, monkeypatch):
        async def busy(_workspace_id):
            return {"repo_slug": "LogosOSS/Logos", "busy": True}

        monkeypatch.setattr(db, "workspace_target", busy)

        with pytest.raises(HTTPException) as refused:
            await main.create_session(
                SessionCreate(workspace_id=3, task="Prove the lemma", team_repository_id=4), principal=_principal()
            )

        assert refused.value.status_code == 409

    async def test_the_repositories_are_listed_for_the_form(self, monkeypatch):
        async def enabled():
            return [dict(LINK, agent_egress_hosts='["github.com"]')]

        monkeypatch.setattr(db, "enabled_coding_repositories", enabled)

        listed = await main.list_coding_repositories(_=_principal())

        assert listed[0].repo_slug == "o/r"
        assert listed[0].agent_egress_hosts == ["github.com"]


# --- forks and closing references ---------------------------------------------


class TestPushTarget:
    @staticmethod
    def _repositories(monkeypatch, answers: dict, created: list | None = None):
        async def get(path, params=None, **_kwargs):
            if path not in answers:
                raise github.GitHubError(f"GET {path} failed (404)", status=404)
            return answers[path]

        async def create(upstream):
            if created is not None:
                created.append(upstream)
            return answers.get("created")

        monkeypatch.setattr(github, "_get", get)
        monkeypatch.setattr(github, "_create_fork", create)

    async def test_a_repository_the_account_may_push_to_is_used_directly(self, monkeypatch):
        self._repositories(monkeypatch, {"/repos/o/r": {"full_name": "o/r", "permissions": {"push": True}}})

        assert await github.push_target("o/r", "LogosOSSAgent") == "o/r"

    async def test_otherwise_the_accounts_fork_is_used(self, monkeypatch):
        self._repositories(
            monkeypatch,
            {
                "/repos/o/r": {"full_name": "o/r", "permissions": {"push": False}},
                "/repos/LogosOSSAgent/r": {
                    "full_name": "LogosOSSAgent/r",
                    "fork": True,
                    "parent": {"full_name": "o/r"},
                },
            },
        )

        assert await github.push_target("o/r", "LogosOSSAgent") == "LogosOSSAgent/r"

    async def test_a_missing_fork_is_created(self, monkeypatch):
        created: list = []
        self._repositories(
            monkeypatch,
            {
                "/repos/o/r": {"full_name": "o/r", "permissions": {}},
                "created": {"full_name": "LogosOSSAgent/r", "fork": True, "parent": {"full_name": "o/r"}},
            },
            created,
        )

        assert await github.push_target("o/r", "LogosOSSAgent") == "LogosOSSAgent/r"
        assert created == ["o/r"]

    async def test_a_same_named_repository_that_is_not_the_fork_is_refused(self, monkeypatch):
        self._repositories(
            monkeypatch,
            {
                "/repos/o/r": {"full_name": "o/r", "permissions": {"push": False}},
                "/repos/LogosOSSAgent/r": {"full_name": "LogosOSSAgent/r", "fork": False},
            },
        )

        with pytest.raises(github.GitHubError):
            await github.push_target("o/r", "LogosOSSAgent")

    def test_the_finalizer_pushes_to_the_fork_and_names_its_owner_as_head(self, monkeypatch):
        monkeypatch.setenv("LOGOS_PUSH_REPO_URL", "https://github.com/LogosOSSAgent/r.git")
        monkeypatch.setenv("LOGOS_PUSH_REPO_SLUG", "LogosOSSAgent/r")

        assert run_session._push_remote() == "push"
        assert run_session._pull_request_head("logos/agent/x") == "LogosOSSAgent:logos/agent/x"

    def test_without_a_fork_the_repository_is_the_remote(self, monkeypatch):
        monkeypatch.delenv("LOGOS_PUSH_REPO_URL", raising=False)
        monkeypatch.delenv("LOGOS_PUSH_REPO_SLUG", raising=False)

        assert run_session._push_remote() == "origin"
        assert run_session._pull_request_head("b") == "b"


class TestClosingReferences:
    def test_several_issues_are_closed_one_line_each(self):
        assert run_session._closed_issues("3,#5, x") == "closes #3\ncloses #5"

    async def test_only_open_issues_assigned_to_the_agent_are_kept(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sessions, "artifact_dir", lambda _sid: tmp_path)
        (tmp_path / "closes.txt").write_text("#1\n2\n3\n4\n")
        issues = {
            1: {"is_pull_request": False, "state": "open", "assignees": ["LogosOSSAgent"]},
            2: {"is_pull_request": False, "state": "open", "assignees": ["someone"]},
            3: {"is_pull_request": False, "state": "closed", "assignees": ["LogosOSSAgent"]},
            4: {"is_pull_request": True, "state": "open", "assignees": ["LogosOSSAgent"]},
        }

        async def summary(number):
            return issues[number]

        async def add_event(*_args, **_kwargs):
            return None

        monkeypatch.setattr(sessions.github, "issue_summary", summary)
        monkeypatch.setattr(sessions.db, "add_event", add_event)
        monkeypatch.setattr(sessions, "settings", replace(sessions.settings, github_login="LogosOSSAgent"))

        kept = await sessions.SessionManager()._claimed_issues(1, {"repo_slug": "o/r"})

        assert kept == ["1"]
