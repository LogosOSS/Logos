"""The link to Logos: parsing, the requests, accepting orders and reporting deployments."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import queued

from logos_node_manager.config import LogosSource, Settings, parse_logos_sources
from logos_node_manager.docker_ops import read_env_value
from logos_node_manager.logos_link import LogosClient, LogosError, LogosLink, Order, parse_orders, snapshot
from logos_node_manager.state import Deployment, Outcome, Phase, StateStore

KEY = "the-secret-deployer-key"


# ---------------------------------------------------------------------- Settings


def test_logos_sources_are_read_from_pairs_of_address_and_key():
    sources = parse_logos_sources("https://dev.example/|key-dev, https://prod.example|key-prod")

    assert [(s.url, s.key) for s in sources] == [
        ("https://dev.example", "key-dev"),
        ("https://prod.example", "key-prod"),
    ]
    assert [s.name for s in sources] == ["dev.example", "prod.example"]


def test_an_entry_with_a_missing_part_or_a_bad_address_is_skipped():
    sources = parse_logos_sources("https://a.example|, |key, ftp://b.example|key, https://c.example|key|more, nonsense")

    assert [(s.url, s.key) for s in sources] == [("https://c.example", "key|more")]


def test_the_first_entry_for_an_address_wins():
    sources = parse_logos_sources("https://a.example|one https://a.example/|two")

    assert [(s.url, s.key) for s in sources] == [("https://a.example", "one")]


def test_the_key_is_not_part_of_the_text_of_a_source():
    assert KEY not in repr(LogosSource(url="https://a.example", key=KEY))


def test_a_core_host_has_its_own_defaults(tmp_path):
    settings = Settings.from_env({"NODE_MANAGER_KIND": "core", "NODE_MANAGER_LOGOS": f"https://prod.example|{KEY}"})

    assert settings.kind == "core"
    assert str(settings.project_dir) == "/opt/logos"
    assert settings.compose_file.name == "docker-compose.yaml"
    assert settings.project_name == "logos"
    assert settings.core_health_url == "https://prod.example/api/info"


def test_the_health_address_can_be_set_and_a_worker_has_none():
    core = Settings.from_env(
        {
            "NODE_MANAGER_KIND": "core",
            "NODE_MANAGER_LOGOS": f"https://a.example|{KEY}",
            "NODE_MANAGER_CORE_HEALTH_URL": "https://b.example/up",
        }
    )
    worker = Settings.from_env({"NODE_MANAGER_LOGOS": f"https://a.example|{KEY}"})

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
                {
                    "id": "plan-1-core",
                    "tag": "v2",
                    "requested_by": "Jane",
                    "plan": {"id": "p1", "position": 0, "extra": [1]},
                },
                {"id": "plan-1-bad", "tag": "bad tag!"},
                {"id": "bad id!", "tag": "v2"},
                "nonsense",
            ]
        }
    )

    assert orders == [Order(id="plan-1-core", tag="v2", requested_by="Jane", plan={"id": "p1", "position": "0"})]
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
    return LogosClient(LogosSource(url=logos.url, key=KEY), kind="core", timeout_margin_s=5.0)


def test_a_poll_sends_the_key_in_a_header_and_returns_the_orders(logos):
    logos.answers = [(200, {"orders": [{"id": "plan-1-core", "tag": "v2", "plan": {"kind": "core"}}]})]

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


def test_an_answer_that_is_not_json_is_an_error(logos):
    logos.answers = [(200, b"<html>")]

    with pytest.raises(LogosError, match="not JSON"):
        client_for(logos).poll(1.0)


def test_an_unreachable_logos_is_a_retryable_error():
    client = LogosClient(LogosSource(url="http://127.0.0.1:9", key=KEY), kind="core", timeout_margin_s=1.0)

    with pytest.raises(LogosError) as caught:
        client.poll(0.1)

    assert caught.value.retryable
    assert KEY not in str(caught.value)


# ---------------------------------------------------------------------- Accepting orders and reporting


class FakeClient:
    """Stands in for a LogosClient when a test is about the link and not the request."""

    def __init__(self, name: str = "dev.example") -> None:
        self.source = LogosSource(url=f"https://{name}", key=KEY)
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
def link(link_settings, store, clock):
    def build(*clients: FakeClient) -> LogosLink:
        return LogosLink(link_settings, store, threading.Event(), list(clients) or [FakeClient()], clock=clock)

    return build


def order(order_id: str = "plan-1-w1", tag: str = "v2", **plan) -> Order:
    return Order(id=order_id, tag=tag, requested_by="Jane Doe", plan={"id": "plan-1", "kind": "worker", **plan})


def test_an_order_becomes_a_queued_record_with_its_source_and_plan(link, store):
    client = FakeClient()

    link(client).accept(client, order())

    record = store.load("plan-1-w1")
    assert record is not None and record.phase is Phase.QUEUED
    assert (record.tag, record.requested_by, record.source) == ("v2", "Jane Doe", "dev.example")
    assert record.plan == {"id": "plan-1", "kind": "worker"}


def test_the_same_order_twice_is_one_deployment(link, store):
    client = FakeClient()
    the_link = link(client)

    the_link.accept(client, order())
    the_link.accept(client, order())

    assert [record.id for record in store.all()] == ["plan-1-w1"]


def test_an_order_for_the_other_kind_of_host_is_rejected(link, store):
    client = FakeClient()
    the_link = link(client)

    the_link.accept(client, order(kind="core"))

    assert store.all() == []
    the_link.report_once(client)
    assert client.sent[0][0]["id"] == "plan-1-w1"
    assert "worker host" in client.sent[0][0]["rejected"]


def test_an_order_whose_id_is_used_for_another_tag_is_rejected(link, store):
    client = FakeClient()
    the_link = link(client)
    the_link.accept(client, order(tag="v2"))

    the_link.accept(client, order(tag="v3"))

    assert store.load("plan-1-w1").tag == "v2"
    the_link.report_once(client)
    assert "already used for tag v2" in next(item for item in client.sent[0] if "rejected" in item)["rejected"]


def test_a_rejection_is_sent_until_logos_takes_it(link):
    client = FakeClient()
    client.acks = set()
    the_link = link(client)
    the_link.accept(client, order(kind="core"))

    the_link.report_once(client)
    assert len(client.sent) == 1
    client.acks = None
    the_link.report_once(client)
    assert len(client.sent) == 2
    the_link.report_once(client)

    # Sent twice, because Logos took it the second time. A third pass has nothing left to send.
    assert len(client.sent) == 2


def test_only_deployments_from_logos_are_reported(link, store):
    client = FakeClient()
    queued(store, deployment_id="by-hand")
    store.submit(
        Deployment(
            id="from-logos", tag="v2", requested_at="2026-01-01T00:00:00+00:00", source="dev.example", plan={"id": "p"}
        )
    )

    link(client).report_once(client)

    assert [item["id"] for item in client.sent[0]] == ["from-logos"]


def test_a_deployment_in_progress_is_sent_again_when_it_changed_or_after_the_interval(link, store, clock):
    client = FakeClient()
    the_link = link(client)
    record = store.submit(Deployment(id="d1", tag="v2", requested_at="2026-01-01T00:00:00+00:00", plan={"id": "p"}))

    the_link.report_once(client)
    the_link.report_once(client)
    assert len(client.sent) == 1

    record.phase = Phase.PULLING
    record.events.append({"at": "2026-01-01T00:00:01+00:00", "phase": "pulling", "note": "x"})
    store.save(record)
    the_link.report_once(client)
    assert len(client.sent) == 2

    clock.sleep(21.0)
    the_link.report_once(client)
    assert len(client.sent) == 3


def finished(store: StateStore, deployment_id: str = "d1") -> Deployment:
    record = store.submit(
        Deployment(id=deployment_id, tag="v2", requested_at="2026-01-01T00:00:00+00:00", plan={"id": "p"})
    )
    record.phase = Phase.DONE
    record.outcome = Outcome.SUCCEEDED
    record.finished_at = "2099-01-01T00:00:00+00:00"
    store.save(record)
    return record


def test_a_finished_deployment_is_sent_until_every_logos_took_it(link, store):
    dev, prod = FakeClient("dev.example"), FakeClient("prod.example")
    the_link = link(dev, prod)
    finished(store)
    prod.acks = set()

    the_link.report_once(dev)
    the_link.report_once(prod)

    assert store.load("d1").final_acked == ["dev.example"]
    the_link.report_once(dev)
    assert len(dev.sent) == 1
    prod.acks = None
    the_link._next_try.clear()
    the_link.report_once(prod)
    assert store.load("d1").final_acked == ["dev.example", "prod.example"]


def test_every_logos_gets_the_same_snapshot_of_a_deployment(link, store):
    dev, prod = FakeClient("dev.example"), FakeClient("prod.example")
    the_link = link(dev, prod)
    finished(store)

    the_link.report_once(dev)
    the_link.report_once(prod)

    assert dev.sent[0] == prod.sent[0]
    assert dev.sent[0][0]["outcome"] == "succeeded"
    assert dev.sent[0][0]["plan"] == {"id": "p"}


def test_a_logos_that_cannot_be_reached_is_asked_again_after_a_growing_pause(link, store, clock):
    client = FakeClient()
    the_link = link(client)
    finished(store)
    client.error = LogosError("dev.example could not be reached")

    the_link.report_once(client)
    client.error = None
    the_link.report_once(client)
    assert client.sent == []

    clock.sleep(6.0)
    the_link.report_once(client)
    assert len(client.sent) == 1


def test_an_old_ending_is_not_reported_any_more(link, store):
    client = FakeClient()
    record = finished(store)
    record.finished_at = "2020-01-01T00:00:00+00:00"
    store.save(record)

    link(client).report_once(client)

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
    record = store.submit(
        Deployment(id="from-logos", tag="v2", requested_at="2026-01-01T00:00:00+00:00", plan={"id": "p"})
    )
    other = queued(store, deployment_id="by-hand")

    deployer.process(record)
    deployer.process(other)
    deployer.report_pending()

    assert [report[0] for report in orchestrator.reports] == ["by-hand"]
