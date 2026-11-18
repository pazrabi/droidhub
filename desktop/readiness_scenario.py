"""DroidHub Desktop M1 independent read-only readiness scenario for one target.

This module provides a pure stdlib readiness runner for checking a single
target device over USB route using an injected synchronous probe.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Stable error codes
USB_DISCONNECTED: str = "USB_DISCONNECTED"
ADB_UNAUTHORIZED: str = "ADB_UNAUTHORIZED"
TIMEOUT: str = "TIMEOUT"
PROBE_FAILED: str = "PROBE_FAILED"
BATTERY_LOW: str = "BATTERY_LOW"

# Step identifiers
STEP_TRANSPORT: str = "transport"
STEP_ANDROID_VERSION: str = "android_version"
STEP_BATTERY: str = "battery"
STEP_CHECK_THRESHOLD: str = "check_threshold"

# Status values
STATUS_SUCCEEDED: str = "succeeded"
STATUS_FAILED: str = "failed"
STATUS_CANCELLED: str = "cancelled"

# Timeouts in seconds for probe operations
TIMEOUT_TRANSPORT: float = 3.0
TIMEOUT_ANDROID_VERSION: float = 5.0
TIMEOUT_BATTERY: float = 5.0

# Set of valid probe error codes
_VALID_PROBE_CODES = frozenset({
    USB_DISCONNECTED,
    ADB_UNAUTHORIZED,
    TIMEOUT,
    PROBE_FAILED,
})


class ProbeError(Exception):
    """Small typed probe exception carrying a stable code only."""

    USB_DISCONNECTED: str = USB_DISCONNECTED
    ADB_UNAUTHORIZED: str = ADB_UNAUTHORIZED
    TIMEOUT: str = TIMEOUT
    PROBE_FAILED: str = PROBE_FAILED

    def __init__(self, code: str) -> None:
        raw_code = getattr(code, "value", code)
        self.code: str = str(raw_code)
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"ProbeError({self.code!r})"


def _never_cancelled() -> bool:
    return False


def _is_valid_battery(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def run_readiness(
    probe: Any,
    device_id: str,
    *,
    min_battery: int = 20,
    cancelled: Optional[Callable[[], bool]] = _never_cancelled,
) -> dict[str, Any]:
    """Execute independent read-only readiness scenario for one target.

    Args:
        probe: Injected synchronous probe with check_transport, android_version,
            and battery_level methods.
        device_id: Identifier of the target device.
        min_battery: Minimum battery percentage threshold (0..100, default 20).
        cancelled: Optional callable returning True if the run should be cancelled.

    Returns:
        Stable JSON-serializable dictionary with readiness execution results.
    """
    if probe is None:
        raise TypeError("probe must be provided")

    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError(f"device_id must be a non-empty string, got {device_id!r}")

    if not _is_valid_battery(min_battery):
        raise ValueError(f"min_battery must be an integer between 0 and 100, got {min_battery!r}")

    is_cancelled: Callable[[], bool] = cancelled if cancelled is not None else _never_cancelled

    steps: list[dict[str, Any]] = []
    last_ok_step: Optional[str] = None
    battery_value: Optional[int] = None

    def _make_result(
        status: str,
        step: str,
        code: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "status": status,
            "step": step,
            "code": code,
            "last_ok_step": last_ok_step,
            "safe_retry": True,
            "route": "usb",
            "steps": steps,
            "battery": battery_value,
        }

    def _extract_error_code(exc: Exception) -> str:
        if isinstance(exc, ProbeError) and exc.code in _VALID_PROBE_CODES:
            return exc.code
        return PROBE_FAILED

    # Step 1: transport
    if is_cancelled():
        return _make_result(STATUS_CANCELLED, STEP_TRANSPORT, None)

    try:
        probe.check_transport(device_id, timeout=TIMEOUT_TRANSPORT)
    except Exception as exc:
        steps.append({"id": STEP_TRANSPORT, "status": STATUS_FAILED})
        return _make_result(STATUS_FAILED, STEP_TRANSPORT, _extract_error_code(exc))

    steps.append({"id": STEP_TRANSPORT, "status": STATUS_SUCCEEDED})
    last_ok_step = STEP_TRANSPORT

    # Step 2: android_version
    if is_cancelled():
        return _make_result(STATUS_CANCELLED, STEP_ANDROID_VERSION, None)

    try:
        ver = probe.android_version(device_id, timeout=TIMEOUT_ANDROID_VERSION)
        if not isinstance(ver, str) or not ver.strip():
            steps.append({"id": STEP_ANDROID_VERSION, "status": STATUS_FAILED})
            return _make_result(STATUS_FAILED, STEP_ANDROID_VERSION, PROBE_FAILED)
    except Exception as exc:
        steps.append({"id": STEP_ANDROID_VERSION, "status": STATUS_FAILED})
        return _make_result(STATUS_FAILED, STEP_ANDROID_VERSION, _extract_error_code(exc))

    version_str = str(ver)
    steps.append({
        "id": STEP_ANDROID_VERSION,
        "status": STATUS_SUCCEEDED,
        "version": version_str,
        "value": version_str,
    })
    last_ok_step = STEP_ANDROID_VERSION

    # Step 3: battery
    if is_cancelled():
        return _make_result(STATUS_CANCELLED, STEP_BATTERY, None)

    try:
        raw_battery = probe.battery_level(device_id, timeout=TIMEOUT_BATTERY)
    except Exception as exc:
        steps.append({"id": STEP_BATTERY, "status": STATUS_FAILED})
        return _make_result(STATUS_FAILED, STEP_BATTERY, _extract_error_code(exc))

    if not _is_valid_battery(raw_battery):
        steps.append({"id": STEP_BATTERY, "status": STATUS_FAILED})
        return _make_result(STATUS_FAILED, STEP_BATTERY, PROBE_FAILED)

    battery_value = raw_battery
    steps.append({
        "id": STEP_BATTERY,
        "status": STATUS_SUCCEEDED,
        "battery": battery_value,
        "level": battery_value,
        "value": battery_value,
    })
    last_ok_step = STEP_BATTERY

    # Step 4: check_threshold
    if is_cancelled():
        return _make_result(STATUS_CANCELLED, STEP_CHECK_THRESHOLD, None)

    if battery_value < min_battery:
        steps.append({"id": STEP_CHECK_THRESHOLD, "status": STATUS_FAILED})
        return _make_result(STATUS_FAILED, STEP_CHECK_THRESHOLD, BATTERY_LOW)

    steps.append({"id": STEP_CHECK_THRESHOLD, "status": STATUS_SUCCEEDED})
    last_ok_step = STEP_CHECK_THRESHOLD
    return _make_result(STATUS_SUCCEEDED, STEP_CHECK_THRESHOLD, None)


__all__ = [
    "ProbeError",
    "run_readiness",
    "USB_DISCONNECTED",
    "ADB_UNAUTHORIZED",
    "TIMEOUT",
    "PROBE_FAILED",
    "BATTERY_LOW",
    "STEP_TRANSPORT",
    "STEP_ANDROID_VERSION",
    "STEP_BATTERY",
    "STEP_CHECK_THRESHOLD",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
]
