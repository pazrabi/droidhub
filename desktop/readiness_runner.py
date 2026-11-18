"""
desktop/readiness_runner.py

Coordinates read-only readiness runs against a set of target devices.

No imports from hub/Desktop internals; the caller injects all dependencies.

Store protocol
--------------
  create_run(*, scenario, request_id, targets, params, parent_run_id=None) -> str
  record_target(run_id, device_id, result) -> None
  get_run(run_id) -> dict | None

check_target protocol
---------------------
  check_target(device_id, *, min_battery, cancelled) -> dict
  The returned dict MUST contain at least the key "status" with a terminal
  string value.  Canonical terminal statuses: "READY", "NOT_READY",
  "PROBE_FAILED", "CANCELLED".

ReadinessRunner public API
--------------------------
  start(target_ids, *, min_battery=20, request_id, parent_run_id=None) -> str
  cancel(run_id) -> bool
  snapshot(run_id) -> dict | None
  close() -> None
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PARALLEL_LIMIT = 2
_SCENARIO = "readiness"


# ---------------------------------------------------------------------------
# ReadinessRunner
# ---------------------------------------------------------------------------


class ReadinessRunner:
    """Coordinates non-blocking readiness runs.

    Parameters
    ----------
    store:
        Object implementing ``create_run``, ``record_target``, ``get_run``.
    check_target:
        Callable that probes a single device.  Signature::

            check_target(device_id, *, min_battery, cancelled) -> dict

        where *cancelled* is a ``threading.Event`` the runner sets when the
        run is cancelled.  The callable MUST return a dict with at least a
        ``"status"`` key.
    """

    def __init__(
        self,
        store: Any,
        check_target: Callable[[str, ...], dict],
    ) -> None:
        self._store = store
        self._check_target = check_target

        self._lock = threading.Lock()
        self._active_run_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}  # run_id -> event

        # Cap at two targets in parallel; at most 1 run is queued in this pool
        # (we enforce one-active-run policy above that).
        self._executor = ThreadPoolExecutor(max_workers=_PARALLEL_LIMIT)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        target_ids: list[str],
        *,
        min_battery: int = 20,
        request_id: str,
        parent_run_id: str | None = None,
    ) -> str:
        """Create and launch a new readiness run.

        Raises ``RuntimeError`` if another run is already active.
        Raises ``ValueError`` if *request_id* was already used by a chained
        run (parent_run_id chain must not repeat the same request_id).
        """
        with self._lock:
            if self._active_run_id is not None:
                raise RuntimeError(
                    f"A run is already active: {self._active_run_id}. "
                    "Call cancel() first or wait for it to finish."
                )

            # Guard against repeated request_id in a parent chain
            if parent_run_id is not None:
                self._assert_request_id_not_in_chain(parent_run_id, request_id)

            run_id = self._store.create_run(
                scenario=_SCENARIO,
                request_id=request_id,
                targets=list(target_ids),
                params={"min_battery": min_battery},
                parent_run_id=parent_run_id,
            )

            cancel_event = threading.Event()
            self._cancel_events[run_id] = cancel_event
            self._active_run_id = run_id

        # Launch in background — does NOT block the caller
        t = threading.Thread(
            target=self._run_worker,
            args=(run_id, list(target_ids), min_battery, cancel_event),
            daemon=True,
            name=f"readiness-{run_id}",
        )
        t.start()

        return run_id

    def cancel(self, run_id: str) -> bool:
        """Signal cancellation for *run_id*.

        Returns ``True`` if the run was found and signalled, ``False`` if it
        was already finished or unknown.
        """
        with self._lock:
            event = self._cancel_events.get(run_id)
            if event is None:
                return False
            event.set()
        return True

    def snapshot(self, run_id: str) -> dict | None:
        """Return the latest stored state of a run, or ``None``."""
        return self._store.get_run(run_id)

    def close(self) -> None:
        """Cancel any active run and shut down the thread pool."""
        with self._lock:
            active = self._active_run_id
            if active is not None:
                ev = self._cancel_events.get(active)
                if ev is not None:
                    ev.set()
        self._executor.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_request_id_not_in_chain(
        self, parent_run_id: str, request_id: str
    ) -> None:
        """Walk the parent chain and raise if *request_id* appears."""
        visited: set[str] = set()
        current_id: str | None = parent_run_id
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            run = self._store.get_run(current_id)
            if run is None:
                break
            if run.get("request_id") == request_id:
                raise ValueError(
                    f"request_id {request_id!r} already appears in the parent chain."
                )
            current_id = run.get("parent_run_id")

    def _run_worker(
        self,
        run_id: str,
        target_ids: list[str],
        min_battery: int,
        cancel_event: threading.Event,
    ) -> None:
        """Execute all target probes (up to _PARALLEL_LIMIT at once)."""
        try:
            futures = {
                self._executor.submit(
                    self._probe_one,
                    run_id,
                    device_id,
                    min_battery,
                    cancel_event,
                ): device_id
                for device_id in target_ids
            }
            for future in futures:
                try:
                    future.result()
                except Exception:
                    # Errors are already caught inside _probe_one; this is an
                    # extra safety net.
                    pass
        finally:
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None
                self._cancel_events.pop(run_id, None)

    def _probe_one(
        self,
        run_id: str,
        device_id: str,
        min_battery: int,
        cancel_event: threading.Event,
    ) -> None:
        """Probe a single device and record the result."""
        # If cancellation was requested before we even start, record immediately
        if cancel_event.is_set():
            self._store.record_target(run_id, device_id, {"status": "CANCELLED"})
            return

        try:
            result = self._check_target(
                device_id,
                min_battery=min_battery,
                cancelled=cancel_event,
            )
            # Normalise: ensure there is always a "status" key
            if not isinstance(result, dict) or "status" not in result:
                result = {"status": "PROBE_FAILED", "error": "malformed result"}
        except Exception as exc:  # noqa: BLE001
            result = {"status": "PROBE_FAILED", "error": str(exc)}

        self._store.record_target(run_id, device_id, result)
