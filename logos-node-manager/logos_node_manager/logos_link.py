"""The link to the Logos that this host obeys: orders in, reports out.

A host obeys one Logos: the one it belongs to. It asks that Logos for orders and tells it how its deployments go.
Nothing calls into the host: the manager only makes outgoing requests. An order is a deployment id, a tag and the kind
of host it is for. The manager writes it to the state directory like any other request, so the queue, the phases and
the limits are the same for an order from the command line and an order from Logos.

Two threads run next to the deployments:

* The *intake* asks for orders. The request waits at Logos until an order appears (long polling), so an order starts
  within seconds and an idle host makes a request about every ``order_wait_s`` seconds. The request is also how Logos
  sees that the host is alive.
* The *reporter* sends the state of the deployments that came from Logos: the deployments in progress at a regular pace,
  a finished one until Logos took it. A Logos that cannot be reached is asked again after a pause that grows. A report
  that fails never changes how a deployment goes.

Only the standard library is used: the manager has no runtime dependencies. The key of the deployer is sent in a header
and never written to a log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from logos_node_manager import __version__
from logos_node_manager.config import Settings
from logos_node_manager.deployer import Deployer
from logos_node_manager.errors import StateError
from logos_node_manager.state import Deployment, StateStore, utc_now, validate_id, validate_tag

logger = logging.getLogger(__name__)

API_PREFIX = "/api/deployments/deployer"
# What of an error answer is kept in a message.
_MAX_DETAIL_CHARS = 200
# Limits for what comes from outside and what goes out, so a record and a report stay small.
MAX_ORDERS_PER_POLL = 20
_MAX_FIELD_CHARS = 300
_MAX_MESSAGE_CHARS = 1500
_MAX_LOG_CHARS = 6000
_MAX_KIND_CHARS = 16
# Asking again after a failure: first pause, and the longest pause.
RETRY_FIRST_S = 5.0
RETRY_MAX_S = 300.0

# The intake never asks faster than this, whatever Logos answers.
MIN_POLL_INTERVAL_S = 1.0
# The reporter looks at the records this often.
REPORT_LOOP_S = 2.0
# A finished deployment is reported while it is this recent.
REPORT_MAX_AGE_S = 24 * 3600.0


def retry_delay(failures: int) -> float:
    """Seconds to wait after ``failures`` failed tries in a row: 5, 10, 20, ... up to ``RETRY_MAX_S``.

    The exponent is bounded before the power is made. A very large power does not fit in a float, and the error would
    end the thread that asks for orders after a few days of failures.
    """
    return min(RETRY_FIRST_S * 2 ** min(max(failures - 1, 0), 16), RETRY_MAX_S)


class LogosError(Exception):
    """Logos gave no usable answer. ``retryable`` tells whether asking again can help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Order:
    id: str
    tag: str
    requested_by: str
    # The kind of host the order is for (``worker`` or ``core``). Empty: not said.
    kind: str


def parse_orders(payload: Any) -> tuple[list[Order], list[dict[str, str]]]:
    """The valid orders of an answer, and one rejection for every order that cannot be run."""
    raw_orders = payload.get("orders") if isinstance(payload, dict) else None
    orders: list[Order] = []
    rejections: list[dict[str, str]] = []
    if not isinstance(raw_orders, list):
        return orders, rejections
    for raw in raw_orders[:MAX_ORDERS_PER_POLL]:
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("id") or "")[:64]
        try:
            orders.append(
                Order(
                    id=validate_id(raw_id),
                    tag=validate_tag(str(raw.get("tag") or "")),
                    requested_by=str(raw.get("requested_by") or "")[:_MAX_FIELD_CHARS],
                    kind=str(raw.get("kind") or "")[:_MAX_KIND_CHARS],
                )
            )
        except ValueError as exc:
            if raw_id:
                rejections.append({"id": raw_id, "rejected": str(exc)[:_MAX_FIELD_CHARS]})
    return orders, rejections


class LogosClient:
    """Requests to Logos."""

    def __init__(self, url: str, key: str, *, kind: str, timeout_margin_s: float) -> None:
        self._url = url
        self._key = key
        self._kind = kind
        self._margin_s = timeout_margin_s

    def poll(self, wait_s: float) -> tuple[list[Order], list[dict[str, str]]]:
        """Ask for orders. Logos answers when one appears, or with none after ``wait_s`` seconds."""
        answer = self._post("/poll", {"wait_s": wait_s}, timeout_s=wait_s + self._margin_s)
        return parse_orders(answer)

    def report(self, deployments: list[dict[str, Any]]) -> set[str]:
        """Send the state of some deployments. Returns the ids that Logos took."""
        answer = self._post("/report", {"deployments": deployments}, timeout_s=self._margin_s + 10.0)
        acked = answer.get("acked")
        return {str(item) for item in acked} if isinstance(acked, list) else set()

    def _post(self, path: str, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        payload = json.dumps({**body, "kind": self._kind, "version": __version__}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._url}{API_PREFIX}{path}",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - http(s) only
                raw = response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or exc.code in (408, 429)
            raise LogosError(f"Logos answered {exc.code} to {path}{self._detail(exc)}", retryable=retryable) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # The text can name the address, but it never holds the key: that is in a header only.
            raise LogosError(f"Logos could not be reached for {path}: {exc}") from None
        try:
            answer = json.loads(raw)
        except ValueError:
            raise LogosError(f"the answer of Logos to {path} is not JSON") from None
        if not isinstance(answer, dict):
            raise LogosError(f"the answer of Logos to {path} is not a JSON object", retryable=False)
        return answer

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(exc.read())
            detail = body.get("detail") or body.get("message")
        except (ValueError, AttributeError, OSError):
            return ""
        return f": {str(detail)[:_MAX_DETAIL_CHARS]}" if isinstance(detail, str) and detail else ""


def snapshot(record: Deployment) -> dict[str, Any]:
    """The state of a deployment as a report carries it."""
    drain_seconds = None
    if record.cordoned_at and record.drained_at:
        drain_seconds = int(Deployer.seconds_between(record.cordoned_at, record.drained_at))
    return {
        "id": record.id,
        "phase": record.phase.value,
        "outcome": record.outcome.value if record.outcome else None,
        "failure": record.failure.value if record.failure else None,
        "message": record.message[:_MAX_MESSAGE_CHARS],
        "requested_by": record.requested_by[:_MAX_FIELD_CHARS],
        "requested_at": record.requested_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "events": [
            {
                "at": event.get("at", ""),
                "phase": event.get("phase", ""),
                "note": str(event.get("note", ""))[:_MAX_FIELD_CHARS],
            }
            for event in record.events
        ],
        "failure_logs": record.failure_logs[-_MAX_LOG_CHARS:],
        "drained_requests": record.drained_requests,
        "drain_seconds": drain_seconds,
        "sole_replica_models": record.sole_replica_models,
    }


def _digest(record: Deployment) -> tuple:
    """What changes when a deployment moves on, cheap to compare."""
    last = record.events[-1].get("at", "") if record.events else ""
    return (record.phase.value, record.outcome.value if record.outcome else None, len(record.events), last)


class LogosLink:
    """The intake and the reporter of a host that obeys a Logos."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        stop: threading.Event,
        client: LogosClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._store = store
        self._stop = stop
        self._client = client
        self._clock = clock
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()
        self._lock = threading.Lock()
        # Orders that cannot be run. They go out with the next report.
        self._rejections: list[dict[str, str]] = []
        # Per deployment: what was sent last and when. Only the reporter reads and writes it.
        self._sent: dict[str, tuple[tuple, float]] = {}
        self._failures = 0
        self._next_try = 0.0
        self._complained: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings, store: StateStore, stop: threading.Event) -> "LogosLink | None":
        """The link, or None when the host obeys no Logos."""
        if not settings.logos_enabled:
            return None
        client = LogosClient(
            settings.logos_url,
            settings.deployer_key,
            kind=settings.kind,
            timeout_margin_s=settings.order_timeout_margin_s,
        )
        return cls(settings, store, stop, client)

    # ------------------------------------------------------------------ Threads

    def start(self) -> None:
        for target, name in ((self._intake_loop, "orders"), (self._report_loop, "reports")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def join(self, timeout_s: float = 2.0) -> None:
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout_s)

    # ------------------------------------------------------------------ Intake

    def _intake_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            began = self._clock()
            try:
                orders, rejections = self._client.poll(self._settings.order_wait_s)
            except LogosError as exc:
                failures += 1
                self._complain(str(exc))
                self._stop.wait(retry_delay(failures))
                continue
            except Exception:  # noqa: BLE001 - an unexpected problem must not end the intake
                failures += 1
                logger.exception("Asking Logos for orders failed unexpectedly")
                self._stop.wait(retry_delay(failures))
                continue
            if failures:
                logger.info("Logos answers again")
            failures = 0
            self._complained = None
            with self._lock:
                self._rejections.extend(rejections)
            for order in orders:
                self.accept(order)
            if orders or rejections:
                self._wake.set()
            spent = self._clock() - began
            if spent < MIN_POLL_INTERVAL_S:
                self._stop.wait(MIN_POLL_INTERVAL_S - spent)

    def accept(self, order: Order) -> None:
        """Write an order to the state directory, or note why it cannot be run."""
        if order.kind and order.kind != self._settings.kind:
            self._reject(order.id, f"this is a {self._settings.kind} host, and the order is for a {order.kind} host")
            return
        try:
            existing = self._store.load(order.id)
            self._store.submit(
                Deployment(
                    id=order.id,
                    tag=order.tag,
                    requested_at=utc_now(),
                    requested_by=order.requested_by,
                    from_logos=True,
                )
            )
        except (ValueError, StateError) as exc:
            self._reject(order.id, str(exc))
            return
        if existing is None:
            logger.info("Order %s from Logos: deploy tag %s", order.id, order.tag)

    def _reject(self, order_id: str, reason: str) -> None:
        logger.warning("Order %s from Logos is refused: %s", order_id, reason)
        with self._lock:
            self._rejections.append({"id": order_id, "rejected": reason[:_MAX_FIELD_CHARS]})

    def _complain(self, message: str) -> None:
        """Log a problem with Logos once, not at every try."""
        if self._complained != message:
            self._complained = message
            logger.warning("%s", message)

    # ------------------------------------------------------------------ Reports

    def _report_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.report_once()
            except Exception:  # noqa: BLE001 - a report must never stop the manager
                logger.exception("Reporting to Logos failed unexpectedly")
            self._wake.wait(REPORT_LOOP_S)
            self._wake.clear()

    def report_once(self) -> None:
        """One pass: send what is due, note what Logos took."""
        now = self._clock()
        if self._next_try > now:
            return
        with self._lock:
            pending_rejections = list(self._rejections)
        due: list[Deployment] = []
        for record in self._store.all():
            if not record.from_logos:
                continue
            if record.finished:
                if record.reported_at is not None or self._too_old(record):
                    continue
                due.append(record)
                continue
            sent = self._sent.get(record.id)
            if sent is None or sent[0] != _digest(record) or now - sent[1] >= self._settings.report_interval_s:
                due.append(record)
        if not due and not pending_rejections:
            return
        payload = [snapshot(record) for record in due] + pending_rejections
        try:
            acked = self._client.report(payload)
        except LogosError as exc:
            self._failures += 1
            self._next_try = now + retry_delay(self._failures)
            self._complain(str(exc))
            return
        self._failures = 0
        self._next_try = 0.0
        self._complained = None
        for record in due:
            if record.id not in acked:
                continue
            if record.finished:
                self._store.mark_reported(record.id)
            else:
                self._sent[record.id] = (_digest(record), now)
        with self._lock:
            for item in pending_rejections:
                if item["id"] in acked and item in self._rejections:
                    self._rejections.remove(item)

    def _too_old(self, record: Deployment) -> bool:
        return Deployer.seconds_between(record.finished_at or utc_now(), utc_now()) > REPORT_MAX_AGE_S
