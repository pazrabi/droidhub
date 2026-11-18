"""
DroidHub Desktop v0.1 – phone card state computation.

Pure stdlib; no I/O, no GUI, no data-access, no device commands.
"""

_VALID_USB_STATES = frozenset(
    {"ready", "unauthorized", "offline", "unverified", "ambiguous", "disconnected"}
)
_VALID_HUB_STATES = frozenset({"available", "stale", "unlinked", "unavailable"})

# Stable reason codes in priority order.
_REASON_ADB_MISSING = "ADB_MISSING"
_REASON_USB_AUTH_REQUIRED = "USB_AUTH_REQUIRED"
_REASON_USB_OFFLINE = "USB_OFFLINE"
_REASON_USB_UNVERIFIED = "USB_UNVERIFIED"
_REASON_USB_AMBIGUOUS = "USB_AMBIGUOUS"
_REASON_USB_DISCONNECTED = "USB_DISCONNECTED"
_REASON_DEVICE_BUSY = "DEVICE_BUSY"
_REASON_SCRCPY_MISSING = "SCRCPY_MISSING"
_REASON_READY = "READY"


def _sanitise_battery(value) -> "int | None":
    """Return an integer 0–100 or None for any invalid/bool input."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0 or value > 100:
        return None
    return value


def describe_phone_state(
    *,
    usb_state: str,
    hub_state: str,
    adb_available: bool,
    scrcpy_available: bool,
    battery: "int | None" = None,
    battery_stale: bool = False,
    busy: bool = False,
) -> dict:
    """
    Compute a UI-ready description of a connected phone's state.

    Parameters
    ----------
    usb_state       : one of ready/unauthorized/offline/unverified/ambiguous/disconnected
    hub_state       : one of available/stale/unlinked/unavailable
    adb_available   : whether the ``adb`` binary is on PATH
    scrcpy_available: whether the ``scrcpy`` binary is on PATH
    battery         : last known battery percentage (0–100 int) or None
    battery_stale   : whether the battery reading is from a previous session
    busy            : whether the device is currently processing a command

    Returns
    -------
    dict with keys:
        usb_state, hub_state, battery, battery_stale, busy,
        can_check, can_open_screen, reason
    """
    if usb_state not in _VALID_USB_STATES:
        raise ValueError(f"Unknown usb_state: {usb_state!r}")
    if hub_state not in _VALID_HUB_STATES:
        raise ValueError(f"Unknown hub_state: {hub_state!r}")

    # Sanitise battery: booleans and out-of-range values become None.
    clean_battery = _sanitise_battery(battery)

    # battery_stale is True when the caller flags it *or* when USB is not
    # ready (so any stored reading is definitely from a past connection).
    usb_ready = usb_state == "ready"
    effective_battery_stale = battery_stale or (not usb_ready and clean_battery is not None)

    # --- Determine reason (first matching rule wins) ---
    if not adb_available:
        reason = _REASON_ADB_MISSING
    elif usb_state == "unauthorized":
        reason = _REASON_USB_AUTH_REQUIRED
    elif usb_state == "offline":
        reason = _REASON_USB_OFFLINE
    elif usb_state == "unverified":
        reason = _REASON_USB_UNVERIFIED
    elif usb_state == "ambiguous":
        reason = _REASON_USB_AMBIGUOUS
    elif usb_state == "disconnected":
        reason = _REASON_USB_DISCONNECTED
    elif busy:
        reason = _REASON_DEVICE_BUSY
    elif not scrcpy_available:
        # USB is ready, ADB is present, device is not busy – usable for ADB
        # checks but cannot open a screen mirror.
        reason = _REASON_SCRCPY_MISSING
    else:
        reason = _REASON_READY

    # --- Capability flags ---
    # can_check: ADB present + USB confirmed ready + not busy
    can_check = adb_available and usb_ready and not busy

    # can_open_screen: everything for can_check plus scrcpy present
    can_open_screen = can_check and scrcpy_available

    return {
        "usb_state": usb_state,
        "hub_state": hub_state,
        "battery": clean_battery,
        "battery_stale": effective_battery_stale,
        "busy": busy,
        "can_check": can_check,
        "can_open_screen": can_open_screen,
        "reason": reason,
    }
