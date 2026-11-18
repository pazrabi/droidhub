"""
tests/test_desktop_readiness_runner.py

Synthetic-probe tests for ReadinessRunner.  No GUI, IPC, ADB, or external I/O.

The fake store and fake check_target are written as strict doubles:
- record_target rejects any status not in {succeeded, failed, cancelled}.
- record_target rejects any key not in the accepted set.
- check_target's cancelled argument is verified to be a callable ()->bool.
- get_run returns ordered [{device_id, status, result?}] target list.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

import pytest

from desktop.readiness_runner import ReadinessRunner


# ---------------------------------------------------------------------------
# Accepted contracts (mirror what production store enforces)
# ---------------------------------------------------------------------------

_ACCEPTED_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_ACCEPTED_RESULT_KEYS = frozenset(
    {"status", "step", "code", "last_ok_step", "safe_retry", "route",
     "steps", "battery", "device_id"}
)

# Aggregate statuses get_run may return
_AGGREGATE_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "partial"}
)


# ---------------------------------------------------------------------------
# Strict in-memory store (test double)
# ---------------------------------------------------------------------------


class StrictInMemoryStore:
    """In-memory store that enforces the production field/key contracts."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        # ordered list of device_ids per run
        self._order: dict[str, list[str]] = {}
        self.record_calls: list[tuple[str, str, dict]] = []

    def create_run(
        self,
        *,
        scenario: str,
        request_id: str,
        targets: list[str],
        params: dict,
        parent_run_id: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        self._runs[run_id] = {
            "run_id": run_id,
            "scenario": scenario,
            "request_id": request_id,
            "targets": list(targets),
            "params": params,
            "parent_run_id": parent_run_id,
            "results": {},  # device_id -> result dict
        }
        self._order[run_id] = list(targets)
        return run_id

    def record_target(self, run_id: str, device_id: str, result: dict) -> None:
        # --- strict contract enforcement ---
        bad_keys = set(result) - _ACCEPTED_RESULT_KEYS
        if bad_keys:
            raise ValueError(
                f"record_target: unexpected keys in result: {bad_keys!r}"
            )
        status = result.get("status")
        if status not in _ACCEPTED_STATUSES:
            raise ValueError(
                f"record_target: status {status!r} not in {_ACCEPTED_STATUSES!r}"
            )
        # ---------------------------------
        self.record_calls.append((run_id, device_id, dict(result)))
        self._runs[run_id]["results"][device_id] = result

    def get_run(self, run_id: str) -> list[dict] | None:
        """Return ordered [{device_id, status, result?}] or None."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        targets = []
        results = run["results"]
        for device_id in self._order[run_id]:
            if device_id in results:
                entry = {
                    "device_id": device_id,
                    "status": results[device_id].get("status", "unknown"),
                    "result": results[device_id],
                }
            else:
                entry = {"device_id": device_id, "status": "queued"}
            targets.append(entry)
        return targets

    # Convenience for chain-walk (used by runner's _assert_request_id_not_in_chain)
    def get_run_meta(self, run_id: str) -> dict | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return {
            "request_id": run["request_id"],
            "parent_run_id": run["parent_run_id"],
        }


# ---------------------------------------------------------------------------
# Strict check_target wrapper
# ---------------------------------------------------------------------------


class StrictCheckTargetWrapper:
    """Wraps a bare fn(device_id, *, min_battery, cancelled) and asserts that
    *cancelled* is a callable ()->bool (not a threading.Event)."""

    def __init__(self, fn: Callable) -> None:
        self._fn = fn

    def __call__(self, device_id: str, *, min_battery: int, cancelled: Any) -> dict:
        # cancelled MUST be callable and return bool
        assert callable(cancelled), (
            f"cancelled must be a callable, got {type(cancelled)!r}"
        )
        result = cancelled()
        assert isinstance(result, bool), (
            f"cancelled() must return bool, got {type(result)!r}"
        )
        return self._fn(device_id, min_battery=min_battery, cancelled=cancelled)


# ---------------------------------------------------------------------------
# Helper: build a runner with strict doubles
# ---------------------------------------------------------------------------


def make_runner(check_target_fn: Callable):
    store = StrictInMemoryStore()
    wrapped = StrictCheckTargetWrapper(check_target_fn)
    runner = ReadinessRunner(store, wrapped)
    return runner, store


def wait_for_run(
    runner: ReadinessRunner,
    run_id: str,
    timeout: float = 5.0,
    poll: float = 0.05,
) -> list[dict]:
    """Poll snapshot until every target has a non-queued, non-running status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = runner.snapshot(run_id)
        if snap is not None:
            pending = [t for t in snap if t["status"] in ("queued", "running")]
            if not pending:
                return snap
        time.sleep(poll)
    snap = runner.snapshot(run_id)
    raise TimeoutError(
        f"Run {run_id} did not finish in {timeout}s. Last snap: {snap}"
    )


# ---------------------------------------------------------------------------
# Test 1: Two-target mixed result — strict statuses
# ---------------------------------------------------------------------------


def test_two_target_mixed_result():
    """One device succeeds, one fails; store receives only accepted keys/statuses."""

    def check_target(device_id, *, min_battery, cancelled):
        if device_id == "device-ok":
            return {"status": "succeeded", "battery": 80}
        return {"status": "failed", "code": "LOW_BATTERY"}

    runner, store = make_runner(check_target)
    try:
        run_id = runner.start(
            ["device-ok", "device-bad"],
            min_battery=20,
            request_id="req-mixed-001",
        )
        snap = wait_for_run(runner, run_id)
    finally:
        runner.close()

    statuses = {t["device_id"]: t["status"] for t in snap}
    assert statuses["device-ok"] == "succeeded"
    assert statuses["device-bad"] == "failed"
    # Ordered — device-ok must come first
    assert snap[0]["device_id"] == "device-ok"
    assert snap[1]["device_id"] == "device-bad"


# ---------------------------------------------------------------------------
# Test 2: Cancellation — cancelled is callable; result status is "cancelled"
# ---------------------------------------------------------------------------


def test_cancellation():
    """Cancelling a run causes pending targets to record status='cancelled'."""

    barrier = threading.Barrier(2)

    def check_target(device_id, *, min_battery, cancelled):
        # cancelled is a callable ()->bool
        barrier.wait(timeout=5)
        # Spin-wait until cancelled() returns True (or timeout)
        deadline = time.monotonic() + 5
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        if cancelled():
            return {"status": "cancelled"}
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    try:
        run_id = runner.start(
            ["device-slow"],
            min_battery=20,
            request_id="req-cancel-001",
        )
        barrier.wait(timeout=5)
        result = runner.cancel(run_id)
        assert result is True, "cancel() should return True for a live run"

        snap = wait_for_run(runner, run_id)
    finally:
        runner.close()

    assert snap[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Test 3: Duplicate start (one active run per runner)
# ---------------------------------------------------------------------------


def test_duplicate_start_raises():
    """Starting a second run while one is active raises RuntimeError."""

    hold = threading.Event()

    def check_target(device_id, *, min_battery, cancelled):
        hold.wait(timeout=5)
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    try:
        runner.start(
            ["device-a"],
            min_battery=20,
            request_id="req-dup-001",
        )
        with pytest.raises(RuntimeError, match="already active"):
            runner.start(
                ["device-b"],
                min_battery=20,
                request_id="req-dup-002",
            )
    finally:
        hold.set()
        runner.close()


# ---------------------------------------------------------------------------
# Test 4: Duplicate request_id in chained runs is rejected
# ---------------------------------------------------------------------------


def test_chained_duplicate_request_id_raises():
    """Using the same request_id in a chain raises ValueError."""

    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "succeeded"}

    store = StrictInMemoryStore()
    wrapped = StrictCheckTargetWrapper(check_target)
    runner = ReadinessRunner(store, wrapped)
    try:
        run_id_1 = runner.start(
            ["device-x"],
            min_battery=20,
            request_id="req-chain-SAME",
        )
        wait_for_run(runner, run_id_1)

        with pytest.raises(ValueError, match="already appears in the parent chain"):
            runner.start(
                ["device-x"],
                min_battery=20,
                request_id="req-chain-SAME",
                parent_run_id=run_id_1,
            )
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Test 5: Device error is isolated (failed / PROBE_FAILED); others still run
# ---------------------------------------------------------------------------


def test_device_error_isolated():
    """An exception from check_target becomes status=failed, code=PROBE_FAILED."""

    def check_target(device_id, *, min_battery, cancelled):
        if device_id == "device-broken":
            raise RuntimeError("ADB exploded")
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    try:
        run_id = runner.start(
            ["device-broken", "device-fine"],
            min_battery=20,
            request_id="req-err-001",
        )
        snap = wait_for_run(runner, run_id)
    finally:
        runner.close()

    statuses = {t["device_id"]: t["status"] for t in snap}
    assert statuses["device-broken"] == "failed"
    assert statuses["device-fine"] == "succeeded"

    broken_result = next(t["result"] for t in snap if t["device_id"] == "device-broken")
    assert broken_result.get("code") == "PROBE_FAILED"
    # Only accepted keys present
    assert set(broken_result.keys()) <= _ACCEPTED_RESULT_KEYS


# ---------------------------------------------------------------------------
# Test 6: snapshot returns None for unknown run_id
# ---------------------------------------------------------------------------


def test_snapshot_unknown_run_id():
    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "succeeded"}

    runner, _ = make_runner(check_target)
    try:
        assert runner.snapshot("nonexistent-id") is None
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Test 7: cancel returns False for unknown run_id
# ---------------------------------------------------------------------------


def test_cancel_unknown_run_id():
    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "succeeded"}

    runner, _ = make_runner(check_target)
    try:
        assert runner.cancel("nonexistent-id") is False
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Test 8: Parallel limit — two targets run concurrently, not sequentially
# ---------------------------------------------------------------------------


def test_parallel_execution():
    """Two targets should run in parallel (finish faster than 2× single time)."""

    delay = 0.3
    start_times: list[float] = []
    lock = threading.Lock()

    def check_target(device_id, *, min_battery, cancelled):
        with lock:
            start_times.append(time.monotonic())
        time.sleep(delay)
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    try:
        t0 = time.monotonic()
        run_id = runner.start(
            ["device-1", "device-2"],
            min_battery=20,
            request_id="req-parallel-001",
        )
        wait_for_run(runner, run_id, timeout=10.0)
        elapsed = time.monotonic() - t0
    finally:
        runner.close()

    assert len(start_times) == 2
    assert elapsed < delay * 1.9, f"Expected parallel execution, took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Test 9: Idempotency — same request_id returns existing run_id, no re-run
# ---------------------------------------------------------------------------


def test_idempotent_start():
    """Starting with a completed request_id returns the existing run_id."""

    call_count = 0

    def check_target(device_id, *, min_battery, cancelled):
        nonlocal call_count
        call_count += 1
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    try:
        run_id_1 = runner.start(
            ["device-x"],
            min_battery=20,
            request_id="req-idem-001",
        )
        wait_for_run(runner, run_id_1)
        first_count = call_count

        run_id_2 = runner.start(
            ["device-x"],
            min_battery=20,
            request_id="req-idem-001",
        )
        # Same run_id returned, no extra probes
        assert run_id_2 == run_id_1
        assert call_count == first_count, "check_target called again on re-start"
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Test 10: get_run returns ordered target list
# ---------------------------------------------------------------------------


def test_get_run_ordered_targets():
    """get_run returns targets in the same order as supplied to start()."""

    order = ["alpha", "beta", "gamma"]

    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    try:
        run_id = runner.start(order, min_battery=20, request_id="req-order-001")
        snap = wait_for_run(runner, run_id)
    finally:
        runner.close()

    assert [t["device_id"] for t in snap] == order
    for entry in snap:
        assert "device_id" in entry
        assert "status" in entry
        assert entry["status"] in _ACCEPTED_STATUSES | {"queued", "running"}


# ---------------------------------------------------------------------------
# Test 11: Store failure is surfaced (not swallowed)
# ---------------------------------------------------------------------------


def test_store_failure_surfaces():
    """If record_target raises, the runner surfaces it via last_store_error."""

    class BrokenStore(StrictInMemoryStore):
        def record_target(self, run_id, device_id, result):
            raise OSError("disk full")

    store = BrokenStore()
    runner = ReadinessRunner(store, StrictCheckTargetWrapper(
        lambda device_id, *, min_battery, cancelled: {"status": "succeeded"}
    ))

    run_id = runner.start(["dev-a"], min_battery=20, request_id="req-store-fail-001")

    # Wait for runner to process (active_run_id clears when worker finishes)
    deadline = time.monotonic() + 5
    while runner._active_run_id is not None and time.monotonic() < deadline:
        time.sleep(0.05)

    runner.close()

    # Store failure is surfaced via last_store_error, not swallowed silently
    err = runner.last_store_error
    assert err is not None, "Store failure must be surfaced on last_store_error"
    assert isinstance(err, OSError)
    assert "disk full" in str(err)


# ---------------------------------------------------------------------------
# Test 12: Close race — no new work after close
# ---------------------------------------------------------------------------


def test_no_start_after_close():
    """start() after close() raises RuntimeError."""

    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "succeeded"}

    runner, _ = make_runner(check_target)
    runner.close()

    with pytest.raises(RuntimeError, match="closed"):
        runner.start(["device-z"], min_battery=20, request_id="req-post-close-001")


# ---------------------------------------------------------------------------
# Test 13: Close joins worker — no use-after-shutdown
# ---------------------------------------------------------------------------


def test_close_joins_worker():
    """close() waits for in-progress probes before shutting the executor down."""

    probe_started = threading.Event()
    probe_unblock = threading.Event()

    def check_target(device_id, *, min_battery, cancelled):
        probe_started.set()
        probe_unblock.wait(timeout=5)
        return {"status": "succeeded"}

    runner, store = make_runner(check_target)
    run_id = runner.start(["dev-b"], min_battery=20, request_id="req-close-join-001")
    probe_started.wait(timeout=5)

    # Unblock and immediately close — close must join before executor shutdown
    probe_unblock.set()
    runner.close()  # Must not raise RuntimeError about executor shutdown

    # After close, snapshot still accessible via store
    snap = store.get_run(run_id)
    assert snap is not None
    assert snap[0]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Test 14: cancelled callable contract — Event.is_set is passed
# ---------------------------------------------------------------------------


def test_cancelled_is_callable():
    """check_target receives cancelled as a callable ()->bool (Event.is_set)."""

    received_cancelled: list[Any] = []

    def check_target(device_id, *, min_battery, cancelled):
        received_cancelled.append(cancelled)
        return {"status": "succeeded"}

    # Use raw (non-strict-wrapped) runner to capture the actual value passed
    store = StrictInMemoryStore()
    runner = ReadinessRunner(store, check_target)
    try:
        run_id = runner.start(["dev-c"], min_battery=20, request_id="req-callable-001")
        wait_for_run(runner, run_id)
    finally:
        runner.close()

    assert len(received_cancelled) == 1
    fn = received_cancelled[0]
    assert callable(fn), f"Expected callable, got {type(fn)!r}"
    result = fn()
    assert isinstance(result, bool), f"Expected bool from cancelled(), got {type(result)!r}"
    assert result is False, "Should not be cancelled"
