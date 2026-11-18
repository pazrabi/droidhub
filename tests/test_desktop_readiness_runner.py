"""
tests/test_desktop_readiness_runner.py

Synthetic-probe tests for ReadinessRunner.  No GUI, IPC, ADB, or external I/O.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from desktop.readiness_runner import ReadinessRunner


# ---------------------------------------------------------------------------
# In-memory store (test double)
# ---------------------------------------------------------------------------


class InMemoryStore:
    """Minimal in-memory implementation of the store protocol."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

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
            "targets": targets,
            "params": params,
            "parent_run_id": parent_run_id,
            "results": {},
        }
        return run_id

    def record_target(self, run_id: str, device_id: str, result: dict) -> None:
        self._runs[run_id]["results"][device_id] = result

    def get_run(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)


# ---------------------------------------------------------------------------
# Helper: build a runner with a synthetic check_target
# ---------------------------------------------------------------------------


def make_runner(check_target_fn):
    store = InMemoryStore()
    runner = ReadinessRunner(store, check_target_fn)
    return runner, store


def wait_for_run(runner, run_id, timeout: float = 5.0, poll: float = 0.05) -> dict:
    """Poll snapshot until the run results cover all targets."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = runner.snapshot(run_id)
        if snap is not None:
            # Done when every target has a recorded result
            if set(snap["results"].keys()) == set(snap["targets"]):
                return snap
        time.sleep(poll)
    snap = runner.snapshot(run_id)
    raise TimeoutError(f"Run {run_id} did not finish in {timeout}s. Last snap: {snap}")


# ---------------------------------------------------------------------------
# Test 1: Two-target mixed result
# ---------------------------------------------------------------------------


def test_two_target_mixed_result():
    """One device is READY, one is NOT_READY; both are recorded correctly."""

    def check_target(device_id, *, min_battery, cancelled):
        if device_id == "device-ok":
            return {"status": "READY", "battery": 80}
        return {"status": "NOT_READY", "reason": "low_battery"}

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

    assert snap["results"]["device-ok"]["status"] == "READY"
    assert snap["results"]["device-bad"]["status"] == "NOT_READY"
    assert snap["scenario"] == "readiness"
    assert snap["request_id"] == "req-mixed-001"


# ---------------------------------------------------------------------------
# Test 2: Cancellation
# ---------------------------------------------------------------------------


def test_cancellation():
    """Cancelling a run causes pending/running targets to record CANCELLED."""

    barrier = threading.Barrier(2)  # synchronise test + probe

    def check_target(device_id, *, min_battery, cancelled):
        # Signal we've started, then block until cancellation arrives
        barrier.wait(timeout=5)
        # Wait for cancellation (or timeout safety)
        cancelled.wait(timeout=5)
        if cancelled.is_set():
            return {"status": "CANCELLED"}
        return {"status": "READY"}

    runner, store = make_runner(check_target)
    try:
        run_id = runner.start(
            ["device-slow"],
            min_battery=20,
            request_id="req-cancel-001",
        )
        # Wait until the probe has started
        barrier.wait(timeout=5)
        result = runner.cancel(run_id)
        assert result is True, "cancel() should return True for a live run"

        snap = wait_for_run(runner, run_id)
    finally:
        runner.close()

    assert snap["results"]["device-slow"]["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# Test 3: Duplicate start (one active run per runner)
# ---------------------------------------------------------------------------


def test_duplicate_start_raises():
    """Starting a second run while one is active raises RuntimeError."""

    # Use an event so the first run blocks until we control it
    hold = threading.Event()

    def check_target(device_id, *, min_battery, cancelled):
        hold.wait(timeout=5)
        return {"status": "READY"}

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
        hold.set()  # unblock the first run
        runner.close()


# ---------------------------------------------------------------------------
# Test 4: Duplicate request_id in chained runs is rejected
# ---------------------------------------------------------------------------


def test_chained_duplicate_request_id_raises():
    """Using the same request_id in a chain raises ValueError."""

    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "READY"}

    store = InMemoryStore()
    runner = ReadinessRunner(store, check_target)
    try:
        # First run — completes quickly (single fast device)
        run_id_1 = runner.start(
            ["device-x"],
            min_battery=20,
            request_id="req-chain-SAME",
        )
        # Wait for it to finish so the runner is free
        wait_for_run(runner, run_id_1)

        # Second run chains off the first with the SAME request_id — must fail
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
# Test 5: Device error is isolated (PROBE_FAILED) and other devices still run
# ---------------------------------------------------------------------------


def test_device_error_isolated():
    """An exception from check_target becomes PROBE_FAILED; others still run."""

    def check_target(device_id, *, min_battery, cancelled):
        if device_id == "device-broken":
            raise RuntimeError("ADB exploded")
        return {"status": "READY"}

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

    assert snap["results"]["device-broken"]["status"] == "PROBE_FAILED"
    assert "ADB exploded" in snap["results"]["device-broken"]["error"]
    assert snap["results"]["device-fine"]["status"] == "READY"


# ---------------------------------------------------------------------------
# Test 6: snapshot returns None for unknown run_id
# ---------------------------------------------------------------------------


def test_snapshot_unknown_run_id():
    def check_target(device_id, *, min_battery, cancelled):
        return {"status": "READY"}

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
        return {"status": "READY"}

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
        return {"status": "READY"}

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

    # Both started, run finished well under 2× the single-probe delay
    assert len(start_times) == 2
    assert elapsed < delay * 1.9, f"Expected parallel execution, took {elapsed:.2f}s"
