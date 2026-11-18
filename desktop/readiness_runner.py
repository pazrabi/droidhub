"""
desktop/readiness_runner.py

Coordinates read-only readiness runs against a set of target devices.

No imports from hub/Desktop internals; the caller injects all dependencies.

Store protocol
--------------
  create_run(*, scenario, request_id, targets, params, parent_run_id=None) -> str
  record_target(run_id, device_id, result) -> None
      result keys (all optional except status): status, step, code,
      last_ok_step, safe_retry, route, steps, battery, device_id
      Accepted statuses: "succeeded", "failed", "cancelled"
  get_run(run_id) -> list[dict] | None
      Returns ordered list of target dicts: [{device_id, status, result?}, ...]
      Aggregate statuses: queued / running / succeeded / failed / cancelled /
      partial.

check_target protocol
---------------------
  check_target(device_id, *, min_battery, cancelled) -> dict
  *cancelled* is a callable () -> bool (pass event.is_set).
  The returned dict MUST conform to the accepted result keys above.
  On exception the runner records status="failed", code="PROBE_FAILED".

ReadinessRunner public API
--------------------------
  start(target_ids, *, min_battery=20, request_id, parent_run_id=None) -> str
      If a completed run with the same request_id already exists (and
      parent_run_id is None), returns its run_id without re-running.
  cancel(run_id) -> bool
  snapshot(run_id) -> list[dict] | None
  close() -> None
  last_store_error -> BaseException | None   (read-only property)
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

# Statuses that check_target / record_target accept
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Keys that record_target accepts (all others are stripped before recording)
_RESULT_KEYS = frozenset(
    {"status", "step", "code", "last_ok_step", "safe_retry", "route",
     "steps", "battery", "device_id"}
)


def _sanitise_result(raw: dict) -> dict:
    """Return a copy of *raw* keeping only the accepted result keys."""
    return {k: v for k, v in raw.items() if k in _RESULT_KEYS}


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

        where *cancelled* is a **callable** ``() -> bool`` (specifically
        ``threading.Event.is_set``).  The callable MUST return a dict whose
        ``status`` is one of ``succeeded``, ``failed``, ``cancelled``.
    """

    def __init__(
        self,
        store: Any,
        check_target: Callable[[str, ...], dict],
    ) -> None:
        self._store = store
        self._check_target = check_target

        self._lock = threading.Lock()
        self._closed = False
        self._active_run_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}  # run_id -> event
        self._worker_thread: threading.Thread | None = None

        # Maps request_id -> run_id for completed runs (idempotency guard).
        # Only populated when parent_run_id is None (simple re-run prevention).
        self._completed_request_ids: dict[str, str] = {}

        # Surfaces the most recent store failure to callers (clear async error).
        self._last_store_error: BaseException | None = None

        # Cap at two targets in parallel; at most 1 run is queued in this pool
        # (we enforce one-active-run policy above that).
        self._executor = ThreadPoolExecutor(max_workers=_PARALLEL_LIMIT)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def last_store_error(self) -> BaseException | None:
        """The most recent exception raised by store.record_target, or None."""
        with self._lock:
            return self._last_store_error

    def start(
        self,
        target_ids: list[str],
        *,
        min_battery: int = 20,
        request_id: str,
        parent_run_id: str | None = None,
    ) -> str:
        """Create and launch a new readiness run.

        Returns the run_id immediately (non-blocking).

        If a completed run for *request_id* already exists **and no
        parent_run_id is supplied**, returns its run_id without re-running
        (idempotent).

        Raises ``RuntimeError`` if the runner has been closed or if another
        run is already active.
        Raises ``ValueError`` if *request_id* was already used in the
        parent chain.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("ReadinessRunner has been closed.")

            # Guard against repeated request_id in a parent chain (before
            # idempotency check so a chain collision is always detected).
            if parent_run_id is not None:
                self._assert_request_id_not_in_chain(parent_run_id, request_id)

            # Idempotency: return existing completed run without re-running
            # (only when there is no parent chain, i.e. a simple duplicate).
            if parent_run_id is None:
                existing_run_id = self._completed_request_ids.get(request_id)
                if existing_run_id is not None:
                    return existing_run_id

            if self._active_run_id is not None:
                raise RuntimeError(
                    f"A run is already active: {self._active_run_id}. "
                    "Call cancel() first or wait for it to finish."
                )

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
            args=(run_id, list(target_ids), min_battery, cancel_event, request_id),
            daemon=True,
            name=f"readiness-{run_id}",
        )
        self._worker_thread = t
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

    def snapshot(self, run_id: str) -> list[dict] | None:
        """Return the latest stored state of a run, or ``None``."""
        return self._store.get_run(run_id)

    def close(self) -> None:
        """Cancel any active run, wait for it to finish, then shut down."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active_run_id
            if active is not None:
                ev = self._cancel_events.get(active)
                if ev is not None:
                    ev.set()

        # Join the worker thread so all probe work has finished before we
        # tear down the executor (prevents use-after-shutdown).
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join()

        self._executor.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_request_id_not_in_chain(
        self, parent_run_id: str, request_id: str
    ) -> None:
        """Walk the parent chain and raise if *request_id* appears.

        Called with ``self._lock`` held.
        """
        visited: set[str] = set()
        current_id: str | None = parent_run_id
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            run = self._store.get_run(current_id)
            if run is None:
                break
            # get_run returns a list of target dicts; request_id is in the
            # run-level metadata which we look up via a helper if the store
            # provides it, or via the internal _runs dict for InMemoryStore.
            # Try get_run_meta first; fall back to dict-style check.
            meta: dict | None = None
            if hasattr(self._store, "get_run_meta"):
                meta = self._store.get_run_meta(current_id)
            elif isinstance(run, dict):
                meta = run

            if meta is not None and meta.get("request_id") == request_id:
                raise ValueError(
                    f"request_id {request_id!r} already appears in the parent chain."
                )
            current_parent = meta.get("parent_run_id") if meta is not None else None
            current_id = current_parent

    def _run_worker(
        self,
        run_id: str,
        target_ids: list[str],
        min_battery: int,
        cancel_event: threading.Event,
        request_id: str,
    ) -> None:
        """Execute all target probes (up to _PARALLEL_LIMIT at once)."""
        store_error: BaseException | None = None
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
                except Exception as exc:  # noqa: BLE001
                    # _probe_one only raises if record_target raised;
                    # surface the first such store error.
                    if store_error is None:
                        store_error = exc
        finally:
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None
                self._cancel_events.pop(run_id, None)
                # Register completion for idempotency (even on error)
                self._completed_request_ids[request_id] = run_id
                if store_error is not None:
                    self._last_store_error = store_error

    def _probe_one(
        self,
        run_id: str,
        device_id: str,
        min_battery: int,
        cancel_event: threading.Event,
    ) -> None:
        """Probe a single device and record the result.

        Raises if ``record_target`` raises (store failure must not be swallowed).
        """
        # If cancellation was requested before we even start, record immediately
        if cancel_event.is_set():
            result = _sanitise_result(
                {"status": "cancelled", "device_id": device_id}
            )
            self._store.record_target(run_id, device_id, result)
            return

        try:
            raw = self._check_target(
                device_id,
                min_battery=min_battery,
                # Pass the callable, not the Event object itself
                cancelled=cancel_event.is_set,
            )
            if not isinstance(raw, dict) or raw.get("status") not in _TERMINAL_STATUSES:
                result = _sanitise_result(
                    {"status": "failed", "code": "PROBE_FAILED",
                     "device_id": device_id}
                )
            else:
                result = _sanitise_result({**raw, "device_id": device_id})
        except Exception:  # noqa: BLE001
            result = _sanitise_result(
                {"status": "failed", "code": "PROBE_FAILED",
                 "device_id": device_id}
            )

        # Let store errors propagate so the worker can surface them
        self._store.record_target(run_id, device_id, result)
