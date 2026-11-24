"""Process-local ownership of worker model lanes during an internal benchmark."""

from asyncio import Future
from collections.abc import Callable
from threading import RLock


class BenchmarkReservations:
    """Keep production traffic off a benchmark's lanes, including between batch runs.

    Ownership follows the provider/model pair so it survives cold loading and
    lane replacement. Already admitted requests drain before benchmark traffic
    starts. New requests require internal authentication. Other models on the
    same worker remain available.
    """

    def __init__(self):
        self._owners: dict[tuple[int, str], int] = {}
        self._nightly_holds: dict[tuple[int, str], int] = {}
        self._lanes: dict[tuple[int, str], tuple[int, str]] = {}
        self._request_guards: dict[str, Callable[[], bool]] = {}
        self._draining: dict[str, tuple[int, str, int]] = {}
        self._requests: dict[str, tuple[int, str, int]] = {}
        self._admitted: dict[str, tuple[int, str]] = {}
        self._lock = RLock()

    def reserve(
        self,
        provider_id: int,
        model: str,
        job_id: int,
        *,
        draining: dict[str, str] | None = None,
        nightly_owner: int | None = None,
    ) -> None:
        with self._lock:
            key = (provider_id, model)
            owner = self._owners.get(key)
            if owner is not None and owner != job_id:
                raise RuntimeError("The model lanes are already reserved for another benchmark")
            nightly_hold = self._nightly_holds.get(key)
            if nightly_hold is not None and nightly_hold != nightly_owner:
                raise RuntimeError("The model lanes are already reserved for nightly benchmarks")
            if nightly_owner is not None and nightly_owner not in {nightly_hold, self._owners.get((provider_id, "*"))}:
                raise RuntimeError("Nightly maintenance no longer owns these model lanes")
            self._owners[key] = job_id
            for request_id, request_model in (draining or {}).items():
                self._draining[request_id] = (provider_id, request_model, job_id)

    def reserve_nightly(
        self, provider_id: int, model: str, owner: int, *, draining: dict[str, str] | None = None
    ) -> None:
        """Keep the model fenced through preparation, individual jobs, and cleanup."""
        with self._lock:
            key = (provider_id, model)
            existing = self._nightly_holds.get(key)
            if existing != owner and (existing is not None or self.owner(provider_id, model) is not None):
                raise RuntimeError("The model lanes are already reserved for another benchmark")
            self._nightly_holds[key] = owner
            for request_id in draining or {}:
                self._draining[request_id] = (provider_id, model, owner)

    def pin_lane(self, provider_id: int, model: str, owner: int, lane_id: str) -> None:
        """Keep nightly requests on the journaled lane even if a replica becomes available."""
        with self._lock:
            if owner not in {self._nightly_holds.get((provider_id, model)), self._owners.get((provider_id, "*"))}:
                raise RuntimeError("Nightly maintenance no longer owns these model lanes")
            self._lanes[(provider_id, model)] = (owner, lane_id)

    def pinned_lane(self, provider_id: int, model: str) -> str | None:
        with self._lock:
            pin = self._lanes.get((provider_id, model))
            return pin[1] if pin else None

    def pinned_model(self, provider_id: int, lane_id: str) -> str | None:
        with self._lock:
            return next(
                (model for (pid, model), (_, lane) in self._lanes.items() if pid == provider_id and lane == lane_id),
                None,
            )

    def try_admit(self, provider_id: int, model: str, request_id: str, reserve_capacity: Callable[[], bool]) -> bool:
        """Atomically admit capacity before a benchmark can take ownership."""
        with self._lock:
            if not self.allows(provider_id, model, request_id) or not reserve_capacity():
                return False
            self._admitted[request_id] = (provider_id, model)
            return True

    def admit_queued(self, provider_id: int, model: str, dequeue: Callable):
        """Keep queue removal and admission atomic with benchmark acquisition."""
        with self._lock:
            owner = self.owner(provider_id, model, exact=True)
            if (owner is not None and owner < 0) or (owner is None and (provider_id, "*") in self._owners):
                return None, None
            task, entry = dequeue(owner)
            if isinstance(task, Future) and not task.done() and entry is not None:
                request_id = getattr(entry, "request_id", None)
                if request_id:
                    self._admitted[request_id] = (provider_id, model)
            return task, entry

    def admitted_requests(self, provider_id: int, model: str) -> set[str]:
        with self._lock:
            return {rid for rid, pair in self._admitted.items() if pair == (provider_id, model)}

    def provider_owner(self, provider_id: int) -> int | None:
        """Include cancelled jobs whose worker cleanup has not finished."""
        with self._lock:
            owners = [owner for (pid, _), owner in self._owners.items() if pid == provider_id]
            owners.extend(owner for (pid, _), owner in self._nightly_holds.items() if pid == provider_id)
            return next((owner for owner in owners if owner > 0), owners[0] if owners else None)

    def owner(self, provider_id: int, model: str, *, exact: bool = False) -> int | None:
        with self._lock:
            key = (provider_id, model)
            return self._owners.get(
                key, self._nightly_holds.get(key, None if exact else self._owners.get((provider_id, "*")))
            )

    def is_nightly_reserved(self, provider_id: int, model: str | None = None) -> bool:
        """Test a given model or, for whole-worker commands, any model on the worker."""
        with self._lock:
            if (provider_id, "*") in self._owners:
                return True
            if model is not None:
                return (provider_id, model) in self._nightly_holds
            return any(pid == provider_id for pid, _ in self._nightly_holds)

    def register_request(
        self, provider_id: int, model: str, job_id: int, request_id: str, *, guard: Callable[[], bool] | None = None
    ) -> None:
        with self._lock:
            if self._owners.get((provider_id, model)) != job_id:
                raise RuntimeError("The benchmark no longer owns the model lanes")
            self._requests[request_id] = (provider_id, model, job_id)
            if guard is not None:
                self._request_guards[request_id] = guard

    def allows(self, provider_id: int, model: str, request_id: str | None = None) -> bool:
        with self._lock:
            guard = self._request_guards.get(request_id)
            if guard is not None and not guard():
                return False
            owner = self.owner(provider_id, model)
            return (
                owner is None
                or self._requests.get(request_id) == (provider_id, model, owner)
                or self._admitted.get(request_id) == (provider_id, model)
                or self._draining.get(request_id) == (provider_id, model, owner)
            )

    def finish_request(self, request_id: str, *, deployment: tuple[int, str] | None = None) -> None:
        """Release request access, optionally only for the completed deployment.

        A reroute admits the destination before releasing the source. Scoped
        cleanup must preserve access already granted on the destination.
        """
        with self._lock:
            draining = self._draining.get(request_id)
            if deployment is None or (draining is not None and draining[:2] == deployment):
                self._draining.pop(request_id, None)
            registered = self._requests.get(request_id)
            if deployment is None or (registered is not None and registered[:2] == deployment):
                self._requests.pop(request_id, None)
                self._request_guards.pop(request_id, None)
            if deployment is None or self._admitted.get(request_id) == deployment:
                self._admitted.pop(request_id, None)

    def release(self, job_id: int) -> None:
        with self._lock:
            self._draining = {key: value for key, value in self._draining.items() if value[2] != job_id}
            self._owners = {key: owner for key, owner in self._owners.items() if owner != job_id}
            self._nightly_holds = {key: owner for key, owner in self._nightly_holds.items() if owner != job_id}
            self._lanes = {key: value for key, value in self._lanes.items() if value[0] != job_id}
            self._requests = {key: value for key, value in self._requests.items() if value[2] != job_id}
            self._request_guards = {key: guard for key, guard in self._request_guards.items() if key in self._requests}


benchmark_reservations = BenchmarkReservations()
