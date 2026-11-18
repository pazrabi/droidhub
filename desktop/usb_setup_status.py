"""
desktop/usb_setup_status.py – Standalone pure-stdlib helper for DroidHub Desktop USB pilot.

No ADB calls, no Tk, no server calls, no raw serial/address in output.
Pure stdlib only; zero third-party imports.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Valid inventory_state values
_INVENTORY_STATES = frozenset({"pending", "ok", "error"})

# Valid output status values (in deterministic priority order, documented below)
_STATUS_VALUES = (
    "adb_missing",
    "checking",
    "adb_error",
    "empty",
    "unauthorized",
    "offline",
    "ambiguous",
    "unverified",
    "unavailable",
    "ready",
    "ready_with_issues",
)

# Regex that identifies emulator serial strings (never counted as USB candidates)
_EMULATOR_RE = re.compile(r"^emulator-\d+$", re.IGNORECASE)

# Regex that identifies host:port network-transport serials (e.g. "192.168.1.5:5555")
_HOST_PORT_RE = re.compile(r".+:\d+$")


def _is_network_serial(serial: str) -> bool:
    """Return True when the serial string itself looks like a network transport address."""
    return bool(_HOST_PORT_RE.match(serial))


def _is_emulator_serial(serial: str) -> bool:
    return bool(_EMULATOR_RE.match(serial))


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Get attribute by name from an object (dataclass, dict, namedtuple…)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def assess_usb_setup(
    *,
    adb_available: bool,
    inventory_state: str,
    devices: Sequence[Any],
) -> dict:
    """
    Assess the USB device setup for the DroidHub Desktop pilot.

    Parameters
    ----------
    adb_available : bool
        Whether the ADB binary/daemon is reachable at all.
    inventory_state : str
        One of ``"pending"``, ``"ok"``, or ``"error"``.
    devices : Sequence[Any]
        Sequence of DeviceInfo-shaped objects with attributes:
        ``serial``, ``state``, ``is_network``, ``usb_info``, ``is_ready``.

    Returns
    -------
    dict with keys:
        status            – one of the STATUS_VALUES strings (see priority table)
        ready_count       – local USB-confirmed ready devices
        unauthorized_count
        offline_count
        ambiguous_count   – duplicate/bad/missing local serial
        unverified_count  – state==device but no USB evidence
        other_count       – any other state not in the above categories

    Priority order for mixed invalid states (most-critical first)
    -------------------------------------------------------------
    1. adb_missing   – ADB binary/daemon not available
    2. checking      – inventory_state == "pending"
    3. adb_error     – inventory_state == "error"
    4. empty         – no local (non-network, non-emulator) candidates found
    5. unauthorized  – at least one unauthorized device
    6. offline       – at least one offline device (no unauthorized)
    7. ambiguous     – duplicate or missing serial, no higher-priority issue
    8. unverified    – state==device but no USB evidence, no higher-priority issue
    9. unavailable   – only other-state devices remain
    10. ready_with_issues – some ready + some non-ready local devices
    11. ready        – all local candidates are ready (and ≥1 exists)
    """
    if inventory_state not in _INVENTORY_STATES:
        raise ValueError(
            f"inventory_state must be one of {sorted(_INVENTORY_STATES)!r},"
            f" got {inventory_state!r}"
        )

    # --- Gate 1: ADB not available -----------------------------------------
    if not adb_available:
        return _zero_counts("adb_missing")

    # --- Gate 2: Inventory not fresh ----------------------------------------
    if inventory_state == "pending":
        return _zero_counts("checking")

    if inventory_state == "error":
        return _zero_counts("adb_error")

    # inventory_state == "ok" from here on -----------------------------------

    # --- Filter to local candidates only ------------------------------------
    # Exclude: network flag set, emulator-* serial, host:port serial.
    local_candidates = []
    for dev in devices:
        serial = _attr(dev, "serial", "") or ""
        is_network = bool(_attr(dev, "is_network", False))

        # Skip network-flagged devices
        if is_network:
            continue
        # Skip emulator serials
        if _is_emulator_serial(serial):
            continue
        # Skip host:port serials (network alias masquerading as local)
        if _is_network_serial(serial):
            continue

        local_candidates.append(dev)

    if not local_candidates:
        return _zero_counts("empty")

    # --- Classify each local candidate --------------------------------------
    # Track seen serials for ambiguity detection
    seen_serials: dict[str, int] = {}  # serial -> count
    for dev in local_candidates:
        serial = _attr(dev, "serial", "") or ""
        seen_serials[serial] = seen_serials.get(serial, 0) + 1

    ready_count = 0
    unauthorized_count = 0
    offline_count = 0
    ambiguous_count = 0
    unverified_count = 0
    other_count = 0

    for dev in local_candidates:
        serial = _attr(dev, "serial", "") or ""
        state = _attr(dev, "state", "") or ""
        is_ready = bool(_attr(dev, "is_ready", False))
        usb_info = _attr(dev, "usb_info", None)

        # Ambiguous: duplicate serial, empty/None/whitespace-only serial,
        # or serial that looks like a network address (belt-and-suspenders).
        serial_stripped = serial.strip()
        is_ambiguous = (
            not serial_stripped
            or seen_serials.get(serial, 0) > 1
        )

        if is_ambiguous:
            ambiguous_count += 1
            continue

        # Classify by ADB-reported state
        state_lower = state.lower().strip()

        if state_lower == "unauthorized":
            # No USB verification claimed – count as unauthorized regardless
            unauthorized_count += 1

        elif state_lower == "offline":
            # No USB verification claimed – count as offline regardless
            offline_count += 1

        elif state_lower == "device":
            # Only count as ready when BOTH is_ready AND explicit non-empty usb_info
            has_usb_evidence = usb_info is not None and str(usb_info).strip() != ""
            if is_ready and has_usb_evidence:
                ready_count += 1
            elif not has_usb_evidence:
                # state==device but no USB evidence → unverified
                unverified_count += 1
            else:
                # is_ready=False but has usb_info → treat as other (unusual)
                other_count += 1

        else:
            # Any other state (recovery, sideload, bootloader, unknown, "")
            other_count += 1

    # --- Derive overall status (priority order) ------------------------------
    total_local = len(local_candidates)
    # Ambiguous devices are already removed from other counts above

    if unauthorized_count > 0:
        status = "unauthorized"
    elif offline_count > 0:
        status = "offline"
    elif ambiguous_count > 0:
        status = "ambiguous"
    elif unverified_count > 0:
        status = "unverified"
    elif other_count > 0 and ready_count == 0:
        status = "unavailable"
    elif ready_count > 0 and (unverified_count + other_count + ambiguous_count + unauthorized_count + offline_count) > 0:
        # ready_with_issues: some ready, some problematic
        status = "ready_with_issues"
    elif ready_count > 0:
        status = "ready"
    else:
        # Everything was ambiguous or other but captured above; fallback
        status = "unavailable"

    return {
        "status": status,
        "ready_count": ready_count,
        "unauthorized_count": unauthorized_count,
        "offline_count": offline_count,
        "ambiguous_count": ambiguous_count,
        "unverified_count": unverified_count,
        "other_count": other_count,
    }


def _zero_counts(status: str) -> dict:
    """Return a result dict with the given status and all counts at zero."""
    return {
        "status": status,
        "ready_count": 0,
        "unauthorized_count": 0,
        "offline_count": 0,
        "ambiguous_count": 0,
        "unverified_count": 0,
        "other_count": 0,
    }
