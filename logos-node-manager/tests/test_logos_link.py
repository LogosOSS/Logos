"""The link to Logos: settings, the requests, accepting orders and reporting deployments."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import queued

from logos_node_manager.config import Settings
from logos_node_manager.docker_ops import read_env_value
from logos_node_manager.logos_link import (
    RETRY_MAX_S,
    LogosClient,
    LogosError,
    LogosLink,
    Order,
    parse_orders,
    retry_delay,
    snapshot,
)
from logos_node_manager.state import Deployment, Outcome, Phase, StateStore

KEY = "the-secret-deployer-key"
LOGOS = {"NODE_MANAGER_LOGOS_URL": "https://a.example", "NODE_MANAGER_DEPLOYER_KEY": KEY}


# ---------------------------------------------------------------------- Settings


def test_the_logos_address_and_the_key_are_read_from_the_environment():
    settings = Settings.from_env({"NODE_MANAGER_LOGOS_URL": "https://dev.example/", "NODE_MANAGER_DEPLOYER_KEY": KEY})

    assert (settings.logos_url, settings.deployer_key) == ("https://dev.example", KEY)
    assert settings.logos_enabled


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"NODE_MANAGER_LOGOS_URL": "https://a.example"},
        {"NODE_MANAGER_DEPLOYER_KEY": KEY},
        {"NODE_MANAGER_LOGOS_URL": "ftp://a.example", "NODE_MANAGER_DEPLOYER_KEY": KEY},
        {"NODE_MANAGER_LOGOS_URL": "nonsense", "NODE_MANAGER_DEPLOYER_KEY": KEY},
    ],
)
def test_a_host_without_a_usable_address_and_key_obeys_no_logos(environ):
    assert not Settings.from_env(environ).logos_enabled


def test_a_worker_is_known_to_logos_by_its_worker_key():
    settings = Settings.from_env({"NODE_MANAGER_LOGOS_URL": "https://a.example", "NODE_MANAGER_NODE_KEY": KEY})

    assert settings.deployer_key == KEY
    assert settings.logos_enabled


def test_a_deployer_key_that_is_set_wins_over_the_worker_key():
    settings = Settings.from_env({**LOGOS, "NODE_MANAGER_NODE_KEY": "the-worker-key"})

    assert settings.deployer_key == KEY


def test_a_core_host_never_takes_a_worker_key_for_its_deployer_key():
    environ = {"NODE_MANAGER_KIND": "core", "NODE_MANAGER_LOGOS_URL": "https://a.example", "NODE_MANAGER_NODE_KEY": KEY}

    settings = Settings.from_env(environ)

    assert settings.deployer_key == ""
    assert not settings.logos_enabled


def test_the_key_is_not_part_of_the_text_of_the_settings():
    assert KEY not in repr(Settings.from_env(LOGOS))
    assert KEY not in repr(
        Settings.from_env({"NODE_MANAGER_LOGOS_URL": "https://a.example", "NODE_MANAGER_NODE_KEY": KEY})
    )


def test_a_core_host_has_its_own_defaults(tmp_path):
    settings = Settings.from_env({"NODE_MANAGER_KIND": "core", **LOGOS})

    assert settings.kind == "core"
    assert str(settings.project_dir) == "/opt/logos"
    assert settings.compose_file.name == "docker-compose.yaml"
    assert settings.project_name == "logos"
    assert settings.core_health_url == "https://a.example/api/info"


def test_the_health_address_can_be_set_and_a_worker_has_none():
    core = Settings.from_env(
        {"NODE_MANAGER_KIND": "core", "NODE_MANAGER_CORE_HEALTH_URL": "https://b.example/up", **LOGOS}
    )
    worker = Settings.from_env(LOGOS)

    assert core.core_health_url == "https://b.example/up"
    assert worker.kind == "worker"
    assert worker.core_health_url == ""
    assert worker.compose_file.name == "docker-compose.yml"


def test_an_unknown_kind_falls_back_to_worker():
    assert Settings.from_env({"NODE_MANAGER_KIND": "banana"}).kind == "worker"


@pytest.mark.parametrize(
    "text, expected",
    [
        ('LOGOS_WEBSERVICE_REPLICAS="2"\n', "2"),
        ("LOGOS_WEBSERVICE_REPLICAS=3\nLOGOS_WEBSERVICE_REPLICAS=4\n", "4"),
        ("LOGOS_WEBSERVICE_REPLICAS='5'\n", "5"),
        ("  LOGOS_WEBSERVICE_REPLICAS = 6 \n", "6"),
        ("OTHER=1\n", None),
        ('LOGOS_WEBSERVICE_REPLICAS="a\\"b"\n', 'a"b'),
    ],
)
def test_a_value_is_read_from_the_env_file(tmp_path, text, expected):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")

    assert read_env_value(path, "LOGOS_WEBSERVICE_REPLICAS") == expected


def test_a_missing_env_file_has_no_values(tmp_path):
    assert read_env_value(tmp_path / "missing", "X") is None


# ---------------------------------------------------------------------- Orders


def test_valid_orders_are_parsed_and_invalid_ones_are_rejected():
    orders, rejections = parse_orders(
        {
            "orders": [
                {"id": "plan-1-core", "tag": "v2", "requested_by": "Jane", "kind": "core"},
                {"id": "plan-1-bad", "tag": "bad tag!"},
                {"id": "bad id!", "tag": "v2"},
                "nonsense",
            ]
        }
    )

    assert orders == [Order(id="plan-1-core", tag="v2", requested_by="Jane", kind="core")]
    assert [item["id"] for item in rejections] == ["plan-1-bad", "bad id!"]
    assert "invalid image tag" in rejections[0]["rejected"]


def test_the_reserved_tag_is_rejected():
    orders, rejections = parse_orders({"orders": [{"id": "x", "tag": "previous"}]})

    assert orders == []
    assert "reserved" in rejections[0]["rejected"]


def test_an_answer_without_orders_has_none():
    assert parse_orders({}) == ([], [])
    assert parse_orders({"orders": "no"}) == ([], [])
    assert parse_orders(["x"]) == ([], [])


# ---------------------------------------------------------------------- The requests


class FakeLogos:
    """A Logos on a local port. A test sets what the next answers are and reads what was asked."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.answers: list[tuple[int, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name the server looks for
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                status, payload = outer.answers.pop(0) if outer.answers else (200, {})
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def logos():
    fake = FakeLogos()
    yield fake
    fake.close()


def client_for(logos: FakeLogos) -> LogosClient:
    return LogosClient(logos.url, KEY, kind="core", timeout_margin_s=5.0)


def test_a_poll_sends_the_key_in_a_header_and_returns_the_orders(logos):
    logos.answers = [(200, {"orders": [{"id": "plan-1-core", "tag": "v2", "kind": "core"}]})]

    orders, rejections = client_for(logos).poll(25.0)

    assert [order.id for order in orders] == ["plan-1-core"]
    request = logos.requests[0]
    assert request["path"] == "/api/deployments/deployer/poll"
    assert request["auth"] == f"Bearer {KEY}"
    assert request["body"]["wait_s"] == 25.0
    assert request["body"]["kind"] == "core"
    assert KEY not in json.dumps(request["body"])


def test_a_report_returns_the_ids_that_logos_took(logos):
    logos.answers = [(200, {"acked": ["a", "b"]})]

    acked = client_for(logos).report([{"id": "a"}, {"id": "b"}, {"id": "c"}])

    assert acked == {"a", "b"}
    assert logos.requests[0]["path"] == "/api/deployments/deployer/report"
    assert [item["id"] for item in logos.requests[0]["body"]["deployments"]] == ["a", "b", "c"]


@pytest.mark.parametrize("status, retryable", [(500, True), (503, True), (429, True), (401, False), (404, False)])
def test_an_error_answer_says_whether_asking_again_can_help(logos, status, retryable):
    logos.answers = [(status, {"detail": "no"})]

    with pytest.raises(LogosError) as caught:
        client_for(logos).poll(1.0)

    assert caught.value.retryable is retryable
    assert str(status) in str(caught.value)
    assert KEY not in str(caught.value)


def test_an_answer_that_is_not_a_json_object_is_a_permanent_error(logos):
    logos.answers = [(200, [1, 2])]

    with pytest.raises(LogosError, match="not a JSON object") as caught:
        client_for(logos).poll(1.0)

    assert caught.value.retryable is False


def test_an_error_answer_without_json_has_no_detail(logos):
    logos.answers = [(500, b"<html>")]

    with pytest.raises(LogosError) as caught:
        client_for(logos).poll(1.0)

    assert caught.value.retryable is True
    assert "500" in str(caught.value)
    assert ":" not in str(caught.value).split("500", 1)[1]


def test_an_answer_that_is_not_json_is_an_error(logos):
    logos.answers = [(200, b"<html>")]

    with pytest.raises(LogosError, match="not JSON"):
        client_for(logos).poll(1.0)


def test_an_unreachable_logos_is_a_retryable_error():
    client = LogosClient("http://127.0.0.1:9", KEY, kind="core", timeout_margin_s=1.0)

    with pytest.raises(LogosError) as caught:
        client.poll(0.1)

    assert caught.value.retryable
    assert KEY not in str(caught.value)


# ---------------------------------------------------------------------- Accepting orders and reporting


class FakeClient:
    """Stands in for a LogosClient when a test is about the link and not the request."""

    def __init__(self) -> None:
        self.sent: list[list[dict]] = []
        self.error: LogosError | None = None
        self.acks: set[str] | None = None

    def report(self, deployments: list[dict]) -> set[str]:
        if self.error:
            raise self.error
        self.sent.append(deployments)
        return {item["id"] for item in deployments} if self.acks is None else self.acks


@pytest.fixture
def link_settings(settings) -> Settings:
    from dataclasses import replace

    return replace(settings, kind="worker", report_interval_s=20.0)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def link(link_settings, store, clock, client) -> LogosLink:
    return LogosLink(link_settings, store, threading.Event(), client, clock=clock)


def order(order_id: str = "plan-1-w1", tag: str = "v2", kind: str = "worker") -> Order:
    return Order(id=order_id, tag=tag, requested_by="Jane Doe", kind=kind)


def from_logos(deployment_id: str = "d1") -> Deployment:
    return Deployment(id=deployment_id, tag="v2", requested_at="2026-01-01T00:00:00+00:00", from_logos=True)


def test_an_order_becomes_a_queued_record_that_came_from_logos(link, store):
    link.accept(order())

    record = store.load("plan-1-w1")
    assert record is not None and record.phase is Phase.QUEUED
    assert (record.tag, record.requested_by, record.from_logos) == ("v2", "Jane Doe", True)


def test_the_same_order_twice_is_one_deployment(link, store):
    link.accept(order())
    link.accept(order())

    assert [record.id for record in store.all()] == ["plan-1-w1"]


def test_an_order_for_the_other_kind_of_host_is_rejected(link, client, store):
    link.accept(order(kind="core"))

    assert store.all() == []
    link.report_once()
    assert client.sent[0][0]["id"] == "plan-1-w1"
    assert "worker host" in client.sent[0][0]["rejected"]


def test_an_order_whose_id_is_used_for_another_tag_is_rejected(link, client, store):
    link.accept(order(tag="v2"))

    link.accept(order(tag="v3"))

    assert store.load("plan-1-w1").tag == "v2"
    link.report_once()
    assert "already used for tag v2" in next(item for item in client.sent[0] if "rejected" in item)["rejected"]


def test_a_rejection_is_sent_until_logos_takes_it(link, client):
    client.acks = set()
    link.accept(order(kind="core"))

    link.report_once()
    assert len(client.sent) == 1
    client.acks = None
    link.report_once()
    assert len(client.sent) == 2
    link.report_once()

    # Sent twice, because Logos took it the second time. A third pass has nothing left to send.
    assert len(client.sent) == 2


def test_only_deployments_from_logos_are_reported(link, client, store):
    queued(store, deployment_id="by-hand")
    store.submit(from_logos("from-logos"))

    link.report_once()

    assert [item["id"] for item in client.sent[0]] == ["from-logos"]


def test_a_deployment_in_progress_is_sent_again_when_it_changed_or_after_the_interval(link, client, store, clock):
    record = store.submit(from_logos())

    link.report_once()
    link.report_once()
    assert len(client.sent) == 1

    record.phase = Phase.PULLING
    record.events.append({"at": "2026-01-01T00:00:01+00:00", "phase": "pulling", "note": "x"})
    store.save(record)
    link.report_once()
    assert len(client.sent) == 2

    clock.sleep(21.0)
    link.report_once()
    assert len(client.sent) == 3


def finished(store: StateStore, deployment_id: str = "d1") -> Deployment:
    record = store.submit(from_logos(deployment_id))
    record.phase = Phase.DONE
    record.outcome = Outcome.SUCCEEDED
    record.finished_at = "2099-01-01T00:00:00+00:00"
    store.save(record)
    return record


def test_a_finished_deployment_is_sent_until_logos_took_it(link, client, store):
    finished(store)
    client.acks = set()

    link.report_once()
    link.report_once()
    assert len(client.sent) == 2
    assert store.load("d1").reported_at is None

    client.acks = None
    link.report_once()
    assert store.load("d1").reported_at is not None
    link.report_once()

    # Sent a third time, because Logos took it then. A fourth pass has nothing left to send.
    assert len(client.sent) == 3


def test_logos_that_cannot_be_reached_is_asked_again_after_a_growing_pause(link, client, store, clock):
    finished(store)
    client.error = LogosError("Logos could not be reached")

    link.report_once()
    client.error = None
    link.report_once()
    assert client.sent == []

    clock.sleep(6.0)
    link.report_once()
    assert len(client.sent) == 1


def test_an_old_ending_is_not_reported_any_more(link, client, store):
    record = finished(store)
    record.finished_at = "2020-01-01T00:00:00+00:00"
    store.save(record)

    link.report_once()

    assert client.sent == []


def test_the_snapshot_carries_the_state_the_page_needs(store):
    record = finished(store)
    record.message = "now running v2"
    record.events.append({"at": "2026-01-01T00:00:00+00:00", "phase": "done", "note": "x" * 1000})
    record.cordoned_at = "2026-01-01T00:00:00+00:00"
    record.drained_at = "2026-01-01T00:00:42+00:00"

    data = snapshot(record)

    assert data["id"] == "d1" and data["phase"] == "done" and data["outcome"] == "succeeded"
    assert data["drain_seconds"] == 42
    assert len(data["events"][0]["note"]) == 300
    assert "key" not in json.dumps(data).lower()


def test_an_ending_from_logos_is_not_reported_to_the_orchestrator(make_deployer, store, orchestrator):
    deployer = make_deployer(orchestrator=orchestrator)
    record = store.submit(from_logos("from-logos"))
    other = queued(store, deployment_id="by-hand")

    deployer.process(record)
    deployer.process(other)
    deployer.report_pending()

    assert [report[0] for report in orchestrator.reports] == ["by-hand"]


# ---------------------------------------------------------------------- The threads


class PollClient(FakeClient):
    """Answers a poll from a script and stops the link when the script is used up."""

    def __init__(self, stop: threading.Event, script: list) -> None:
        super().__init__()
        self.stop = stop
        self.script = list(script)

    def poll(self, wait_s: float):
        step = self.script.pop(0)
        if not self.script:
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        return step


def intake(link_settings, store, clock, script):
    stop = threading.Event()
    client = PollClient(stop, script)
    link = LogosLink(link_settings, store, stop, client, clock=clock)
    return link, client, stop


def test_the_link_exists_only_for_a_host_that_obeys_a_logos(settings, store):
    stop = threading.Event()

    assert LogosLink.from_settings(settings, store, stop) is None
    assert LogosLink.from_settings(Settings.from_env(LOGOS), store, stop) is not None


def test_the_intake_accepts_orders_and_keeps_rejections_for_the_next_report(link_settings, store, clock):
    link, _, _ = intake(
        link_settings,
        store,
        clock,
        [([order("o1")], [{"id": "o2", "rejected": "bad tag"}])],
    )

    link._intake_loop()

    assert store.load("o1") is not None
    assert link._rejections == [{"id": "o2", "rejected": "bad tag"}]
    assert link._wake.is_set()


def test_the_intake_goes_on_after_an_error_of_logos(link_settings, store, clock):
    link, client, _ = intake(link_settings, store, clock, [LogosError("down"), LogosError("down"), ([], [])])
    link._stop.wait = lambda timeout=None: False  # no real pause between the tries

    link._intake_loop()

    assert client.script == []
    assert link._complained is None


def test_the_intake_goes_on_after_an_unexpected_error(link_settings, store, clock):
    link, client, _ = intake(link_settings, store, clock, [RuntimeError("boom"), ([], [])])
    link._stop.wait = lambda timeout=None: False

    link._intake_loop()

    assert client.script == []


def test_a_problem_with_logos_is_logged_once(link, caplog):
    with caplog.at_level("WARNING"):
        link._complain("down")
        link._complain("down")
        link._complain("other")

    assert [r.getMessage() for r in caplog.records] == ["down", "other"]


def test_a_report_that_fails_unexpectedly_does_not_stop_the_reporter(link_settings, store, clock):
    link, client, stop = intake(link_settings, store, clock, [([], [])])

    def explode():
        stop.set()
        raise RuntimeError("boom")

    link.report_once = explode
    link._wake.set()

    link._report_loop()

    assert stop.is_set()


def test_the_threads_start_and_end_when_the_link_stops(link_settings, store, clock):
    link, client, stop = intake(link_settings, store, clock, [([], [])])

    link.start()
    link.join(timeout_s=5.0)

    assert stop.is_set()
    assert all(not thread.is_alive() for thread in link._threads)


def test_the_retry_delay_grows_to_its_limit_and_never_overflows():
    assert [retry_delay(n) for n in (1, 2, 3)] == [5.0, 10.0, 20.0]
    assert retry_delay(7) == RETRY_MAX_S
    # The power of two for the 1025th failure does not fit in a float.
    assert retry_delay(1025) == RETRY_MAX_S
    assert retry_delay(10**9) == RETRY_MAX_S
