"""Tests for DroidHub Desktop M1 readiness scenario."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional
import pytest

from desktop.readiness_scenario import (
    ADB_UNAUTHORIZED,
    BATTERY_LOW,
    PROBE_FAILED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STEP_ANDROID_VERSION,
    STEP_BATTERY,
    STEP_CHECK_THRESHOLD,
    STEP_TRANSPORT,
    TIMEOUT,
    USB_DISCONNECTED,
    ProbeError,
    run_readiness,
)


class FakeProbe:
    """Configurable synchronous probe mock for testing readiness scenarios."""

    def __init__(
        self,
        *,
        transport_exc: Optional[Exception] = None,
        version_val: str = "14",
        version_exc: Optional[Exception] = None,
        battery_val: Any = 85,
        battery_exc: Optional[Exception] = None,
    ) -> None:
        self.transport_exc = transport_exc
        self.version_val = version_val
        self.version_exc = version_exc
        self.battery_val = battery_val
        self.battery_exc = battery_exc

        self.transport_calls: list[tuple[str, dict[str, Any]]] = []
        self.version_calls: list[tuple[str, dict[str, Any]]] = []
        self.battery_calls: list[tuple[str, dict[str, Any]]] = []

    def check_transport(self, device_id: str, *, timeout: float) -> None:
        self.transport_calls.append((device_id, {"timeout": timeout}))
        if self.transport_exc is not None:
            raise self.transport_exc

    def android_version(self, device_id: str, *, timeout: float) -> str:
        self.version_calls.append((device_id, {"timeout": timeout}))
        if self.version_exc is not None:
            raise self.version_exc
        return self.version_val

    def battery_level(self, device_id: str, *, timeout: float) -> int:
        self.battery_calls.append((device_id, {"timeout": timeout}))
        if self.battery_exc is not None:
            raise self.battery_exc
        return self.battery_val


def test_success_full_run() -> None:
    """Test full successful readiness scenario with all steps completing."""
    probe = FakeProbe(version_val="14", battery_val=85)
    res = run_readiness(probe, "dev_001", min_battery=20)

    # Top-level result contract
    assert res["device_id"] == "dev_001"
    assert res["status"] == STATUS_SUCCEEDED
    assert res["step"] == STEP_CHECK_THRESHOLD
    assert res["code"] is None
    assert res["last_ok_step"] == STEP_CHECK_THRESHOLD
    assert res["safe_retry"] is True
    assert res["route"] == "usb"
    assert res["battery"] == 85

    # Steps verification
    steps = res["steps"]
    assert len(steps) == 4
    assert steps[0] == {"id": STEP_TRANSPORT, "status": STATUS_SUCCEEDED}
    assert steps[1]["id"] == STEP_ANDROID_VERSION
    assert steps[1]["status"] == STATUS_SUCCEEDED
    assert steps[1]["version"] == "14"
    assert steps[1]["value"] == "14"
    assert steps[2]["id"] == STEP_BATTERY
    assert steps[2]["status"] == STATUS_SUCCEEDED
    assert steps[2]["battery"] == 85
    assert steps[2]["value"] == 85
    assert steps[3] == {"id": STEP_CHECK_THRESHOLD, "status": STATUS_SUCCEEDED}

    # Timeouts verification: module must pass 3, 5, 5 seconds respectively
    assert len(probe.transport_calls) == 1
    assert probe.transport_calls[0] == ("dev_001", {"timeout": 3.0})
    assert len(probe.version_calls) == 1
    assert probe.version_calls[0] == ("dev_001", {"timeout": 5.0})
    assert len(probe.battery_calls) == 1
    assert probe.battery_calls[0] == ("dev_001", {"timeout": 5.0})

    # JSON serialization roundtrip
    serialized = json.dumps(res)
    assert json.loads(serialized) == res


def test_cancellation_before_first_step() -> None:
    """Test cancellation before transport check begins."""
    probe = FakeProbe()
    res = run_readiness(probe, "dev_001", cancelled=lambda: True)

    assert res["device_id"] == "dev_001"
    assert res["status"] == STATUS_CANCELLED
    assert res["step"] == STEP_TRANSPORT
    assert res["code"] is None
    assert res["last_ok_step"] is None
    assert res["safe_retry"] is True
    assert res["route"] == "usb"
    assert res["steps"] == []
    assert res["battery"] is None

    # Probe must never be touched
    assert len(probe.transport_calls) == 0
    assert len(probe.version_calls) == 0
    assert len(probe.battery_calls) == 0


def test_cancellation_before_android_version() -> None:
    """Test cancellation after transport succeeds but before OS version check."""
    calls = 0

    def cancel_after_transport() -> bool:
        return len(probe.transport_calls) >= 1

    probe = FakeProbe()
    res = run_readiness(probe, "dev_001", cancelled=cancel_after_transport)

    assert res["status"] == STATUS_CANCELLED
    assert res["step"] == STEP_ANDROID_VERSION
    assert res["code"] is None
    assert res["last_ok_step"] == STEP_TRANSPORT
    assert res["battery"] is None

    # Steps should only contain the succeeded transport step
    assert len(res["steps"]) == 1
    assert res["steps"][0] == {"id": STEP_TRANSPORT, "status": STATUS_SUCCEEDED}

    # Subsequent probe methods not called
    assert len(probe.transport_calls) == 1
    assert len(probe.version_calls) == 0
    assert len(probe.battery_calls) == 0


def test_cancellation_before_battery() -> None:
    """Test cancellation after OS version succeeds but before battery check."""
    probe = FakeProbe(version_val="13")

    def cancel_after_os() -> bool:
        return len(probe.version_calls) >= 1

    res = run_readiness(probe, "dev_001", cancelled=cancel_after_os)

    assert res["status"] == STATUS_CANCELLED
    assert res["step"] == STEP_BATTERY
    assert res["code"] is None
    assert res["last_ok_step"] == STEP_ANDROID_VERSION
    assert res["battery"] is None

    assert len(res["steps"]) == 2
    assert res["steps"][0]["id"] == STEP_TRANSPORT
    assert res["steps"][1]["id"] == STEP_ANDROID_VERSION

    assert len(probe.transport_calls) == 1
    assert len(probe.version_calls) == 1
    assert len(probe.battery_calls) == 0


def test_cancellation_before_check_threshold() -> None:
    """Test cancellation after battery succeeds but before check_threshold."""
    probe = FakeProbe(battery_val=60)

    def cancel_after_battery() -> bool:
        return len(probe.battery_calls) >= 1

    res = run_readiness(probe, "dev_001", cancelled=cancel_after_battery)

    assert res["status"] == STATUS_CANCELLED
    assert res["step"] == STEP_CHECK_THRESHOLD
    assert res["code"] is None
    assert res["last_ok_step"] == STEP_BATTERY
    assert res["battery"] == 60

    assert len(res["steps"]) == 3
    assert res["steps"][0]["id"] == STEP_TRANSPORT
    assert res["steps"][1]["id"] == STEP_ANDROID_VERSION
    assert res["steps"][2]["id"] == STEP_BATTERY


def test_failed_battery_read_after_os_timeout() -> None:
    """Test failure when battery read times out after transport and OS succeeded."""
    probe = FakeProbe(
        version_val="14",
        battery_exc=ProbeError(TIMEOUT),
    )
    res = run_readiness(probe, "dev_001")

    assert res["status"] == STATUS_FAILED
    assert res["step"] == STEP_BATTERY
    assert res["code"] == TIMEOUT
    assert res["last_ok_step"] == STEP_ANDROID_VERSION
    assert res["safe_retry"] is True
    assert res["route"] == "usb"
    assert res["battery"] is None

    assert len(res["steps"]) == 3
    assert res["steps"][0] == {"id": STEP_TRANSPORT, "status": STATUS_SUCCEEDED}
    assert res["steps"][1]["id"] == STEP_ANDROID_VERSION
    assert res["steps"][1]["status"] == STATUS_SUCCEEDED
    assert res["steps"][2] == {"id": STEP_BATTERY, "status": STATUS_FAILED}


def test_failed_battery_read_disconnect_and_unauthorized() -> None:
    """Test battery read failure with USB disconnect and ADB unauthorized codes."""
    # Disconnect
    probe_disc = FakeProbe(battery_exc=ProbeError(USB_DISCONNECTED))
    res_disc = run_readiness(probe_disc, "dev_001")
    assert res_disc["status"] == STATUS_FAILED
    assert res_disc["step"] == STEP_BATTERY
    assert res_disc["code"] == USB_DISCONNECTED
    assert res_disc["last_ok_step"] == STEP_ANDROID_VERSION

    # Unauthorized
    probe_unauth = FakeProbe(battery_exc=ProbeError(ADB_UNAUTHORIZED))
    res_unauth = run_readiness(probe_unauth, "dev_001")
    assert res_unauth["status"] == STATUS_FAILED
    assert res_unauth["step"] == STEP_BATTERY
    assert res_unauth["code"] == ADB_UNAUTHORIZED
    assert res_unauth["last_ok_step"] == STEP_ANDROID_VERSION


def test_threshold_boundary() -> None:
    """Test boundary conditions for battery threshold check."""
    # Exactly at threshold (battery == min_battery): must succeed
    probe_eq = FakeProbe(battery_val=20)
    res_eq = run_readiness(probe_eq, "dev_001", min_battery=20)
    assert res_eq["status"] == STATUS_SUCCEEDED
    assert res_eq["step"] == STEP_CHECK_THRESHOLD
    assert res_eq["code"] is None
    assert res_eq["last_ok_step"] == STEP_CHECK_THRESHOLD
    assert res_eq["battery"] == 20

    # One below threshold (battery == min_battery - 1): must fail with BATTERY_LOW
    probe_below = FakeProbe(battery_val=19)
    res_below = run_readiness(probe_below, "dev_001", min_battery=20)
    assert res_below["status"] == STATUS_FAILED
    assert res_below["step"] == STEP_CHECK_THRESHOLD
    assert res_below["code"] == BATTERY_LOW
    assert res_below["last_ok_step"] == STEP_BATTERY
    assert res_below["battery"] == 19
    assert res_below["steps"][-1] == {"id": STEP_CHECK_THRESHOLD, "status": STATUS_FAILED}

    # One above threshold
    probe_above = FakeProbe(battery_val=21)
    res_above = run_readiness(probe_above, "dev_001", min_battery=20)
    assert res_above["status"] == STATUS_SUCCEEDED

    # Boundary at 0
    probe_zero = FakeProbe(battery_val=0)
    res_zero = run_readiness(probe_zero, "dev_001", min_battery=0)
    assert res_zero["status"] == STATUS_SUCCEEDED
    assert res_zero["battery"] == 0

    # Boundary at 100
    probe_max = FakeProbe(battery_val=100)
    res_max = run_readiness(probe_max, "dev_001", min_battery=100)
    assert res_max["status"] == STATUS_SUCCEEDED
    assert res_max["battery"] == 100

    probe_max_fail = FakeProbe(battery_val=99)
    res_max_fail = run_readiness(probe_max_fail, "dev_001", min_battery=100)
    assert res_max_fail["status"] == STATUS_FAILED
    assert res_max_fail["code"] == BATTERY_LOW


@pytest.mark.parametrize("invalid_battery", [
    True,       # bool is non-int
    False,      # bool is non-int
    -1,         # below 0
    101,        # above 100
    "85",       # string
    None,       # NoneType
    50.5,       # float
])
def test_validation_battery_level(invalid_battery: Any) -> None:
    """Test integer non-bool 0..100 validation on probe battery value."""
    probe = FakeProbe(battery_val=invalid_battery)
    res = run_readiness(probe, "dev_001")

    assert res["status"] == STATUS_FAILED
    assert res["step"] == STEP_BATTERY
    assert res["code"] == PROBE_FAILED
    assert res["last_ok_step"] == STEP_ANDROID_VERSION
    assert res["battery"] is None
    assert len(res["steps"]) == 3
    assert res["steps"][-1] == {"id": STEP_BATTERY, "status": STATUS_FAILED}


@pytest.mark.parametrize("invalid_min_battery", [
    True,
    False,
    -1,
    101,
    "20",
    None,
])
def test_validation_min_battery_argument(invalid_min_battery: Any) -> None:
    """Test min_battery parameter validation rejects non-int, bool, out-of-range."""
    probe = FakeProbe()
    with pytest.raises(ValueError, match="min_battery"):
        run_readiness(probe, "dev_001", min_battery=invalid_min_battery)


def test_validation_device_id_and_probe_arguments() -> None:
    """Test device_id and probe parameter validation."""
    probe = FakeProbe()
    with pytest.raises(ValueError, match="device_id"):
        run_readiness(probe, "")
    with pytest.raises(ValueError, match="device_id"):
        run_readiness(probe, "   ")
    with pytest.raises(ValueError, match="device_id"):
        run_readiness(probe, None)  # type: ignore

    with pytest.raises(TypeError, match="probe"):
        run_readiness(None, "dev_001")


def test_no_subsequent_steps_after_transport_failure() -> None:
    """Test no subsequent steps are executed if transport step fails."""
    probe = FakeProbe(transport_exc=ProbeError(USB_DISCONNECTED))
    res = run_readiness(probe, "dev_001")

    assert res["status"] == STATUS_FAILED
    assert res["step"] == STEP_TRANSPORT
    assert res["code"] == USB_DISCONNECTED
    assert res["last_ok_step"] is None
    assert len(res["steps"]) == 1
    assert res["steps"][0] == {"id": STEP_TRANSPORT, "status": STATUS_FAILED}

    # Subsequent probe methods must NOT be called
    assert len(probe.transport_calls) == 1
    assert len(probe.version_calls) == 0
    assert len(probe.battery_calls) == 0


def test_no_subsequent_steps_after_os_failure() -> None:
    """Test no subsequent steps are executed if OS version step fails."""
    probe = FakeProbe(version_exc=ProbeError(ADB_UNAUTHORIZED))
    res = run_readiness(probe, "dev_001")

    assert res["status"] == STATUS_FAILED
    assert res["step"] == STEP_ANDROID_VERSION
    assert res["code"] == ADB_UNAUTHORIZED
    assert res["last_ok_step"] == STEP_TRANSPORT
    assert len(res["steps"]) == 2
    assert res["steps"][1] == {"id": STEP_ANDROID_VERSION, "status": STATUS_FAILED}

    assert len(probe.transport_calls) == 1
    assert len(probe.version_calls) == 1
    assert len(probe.battery_calls) == 0


def test_no_raw_exception_text_leak() -> None:
    """Test that raw exception messages or tracebacks never leak into results."""
    secret_text = "SECRET_TOKEN_DO_NOT_LEAK_987654321"
    probe = FakeProbe(transport_exc=RuntimeError(secret_text))
    res = run_readiness(probe, "dev_001")

    assert res["status"] == STATUS_FAILED
    assert res["step"] == STEP_TRANSPORT
    assert res["code"] == PROBE_FAILED

    serialized = json.dumps(res)
    assert secret_text not in serialized


def test_probe_error_typed_contract() -> None:
    """Verify ProbeError class behavior and stable codes."""
    err = ProbeError(TIMEOUT)
    assert isinstance(err, Exception)
    assert err.code == TIMEOUT
    assert str(err) == TIMEOUT
    assert repr(err) == f"ProbeError({TIMEOUT!r})"

    # Verify error code constants are available on ProbeError
    assert ProbeError.USB_DISCONNECTED == USB_DISCONNECTED
    assert ProbeError.ADB_UNAUTHORIZED == ADB_UNAUTHORIZED
    assert ProbeError.TIMEOUT == TIMEOUT
    assert ProbeError.PROBE_FAILED == PROBE_FAILED


def test_no_automatic_retries() -> None:
    """Verify scenario executes synchronously without automatic retries."""
    probe = FakeProbe(transport_exc=ProbeError(TIMEOUT))
    res = run_readiness(probe, "dev_001")

    assert res["status"] == STATUS_FAILED
    assert len(probe.transport_calls) == 1  # exactly once, no retry
