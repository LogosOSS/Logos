"""Warm lane selection for the simple residency policy."""

from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple

from logos.logosnode_snapshot import _lane_served_context_window
from logos.terminal_logging import lane_metric_float


class WarmLaneRank(NamedTuple):
    context_shortfall: int
    waiting: float
    occupied_fraction: float
    turn: int


class LaneBooking(NamedTuple):
    lane_id: str
    revision: int
    context_tokens: int


def unqueued_context_limit(lanes, profiles: dict) -> float:
    """Largest request whose best-fitting lane has no engine backlog.

    An unknown context window accepts any estimate. When every known window
    is too narrow the widest lane gets the engine's final say.
    """
    widest = free = 0
    for lane in lanes:
        window = _lane_served_context_window(lane, profiles) or float("inf")
        widest = max(widest, window)
        if not engine_waiting(lane):
            free = max(free, window)
    return float("inf") if free and free == widest else free


def engine_waiting(lane: dict) -> float:
    metrics = lane.get("backend_metrics")
    return (lane_metric_float(metrics.get("queue_waiting")) or 0) if isinstance(metrics, dict) else 0


@dataclass
class WarmLaneBookings:
    """Dispatch state for one worker session. Lane IDs are local to the worker."""

    rounds: dict[str, int] = field(default_factory=dict)
    requests: dict[str, LaneBooking] = field(default_factory=dict)
    active: Counter[str] = field(default_factory=Counter)
    fresh: Counter[str] = field(default_factory=Counter)

    def observe(self, lanes: list) -> None:
        self.fresh.clear()
        live = {str(lane.get("lane_id")) for lane in lanes if isinstance(lane, dict)}
        self.rounds = {lid: turn for lid, turn in self.rounds.items() if lid in live}

    def rank(self, lane: dict, round_floor: int, context_tokens: int, profiles: dict) -> WarmLaneRank:
        lid = str(lane["lane_id"])
        backend = lane.get("backend_metrics") if isinstance(lane.get("backend_metrics"), dict) else {}
        running = max(
            lane_metric_float(backend.get("requests_running")) or 0,
            lane_metric_float(lane.get("active_requests")) or 0,
            self.active[lid] - self.fresh[lid],
        )
        parallel = max(1, lane_metric_float(lane.get("num_parallel")) or 0)
        window = _lane_served_context_window(lane, profiles) if context_tokens else 0
        shortfall = max(0, context_tokens - window) if window else 0
        return WarmLaneRank(
            shortfall,
            engine_waiting(lane),
            (running + self.fresh[lid]) / parallel,
            max(round_floor, self.rounds.get(lid, 0)),
        )

    def book(self, request_id: str, lid: str, revision: int, turn: int, context_tokens: int) -> None:
        self.release(request_id, revision)
        self.requests[request_id] = LaneBooking(lid, revision, context_tokens)
        self.active[lid] += 1
        self.fresh[lid] += 1
        self.rounds[lid] = turn

    def release(self, request_id: str, revision: int) -> None:
        previous = self.requests.pop(request_id, None)
        if previous is None:
            return
        lid = previous.lane_id
        self.active[lid] -= 1
        if not self.active[lid]:
            del self.active[lid]
        if revision == previous.revision:
            self.fresh[lid] -= 1
            if not self.fresh[lid]:
                del self.fresh[lid]
