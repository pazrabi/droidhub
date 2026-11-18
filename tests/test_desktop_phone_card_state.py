"""Tests for desktop/phone_card_state.py – DroidHub Desktop v0.1."""

import pytest
from desktop.phone_card_state import describe_phone_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(**overrides):
    """Return a fully-valid base call with all binaries present and USB ready."""
    base = dict(
        usb_state="ready",
        hub_state="available",
        adb_available=True,
        scrcpy_available=True,
    )
    base.update(overrides)
    return describe_phone_state(**base)


# ---------------------------------------------------------------------------
# READY path
# ---------------------------------------------------------------------------

class TestReadyPath:
    def test_all_good(self):
        r = _state()
        assert r["reason"] == "READY"
        assert r["can_check"] is True
        assert r["can_open_screen"] is True

    def test_copies_usb_and_hub_state(self):
        r = _state(hub_state="stale")
        assert r["usb_state"] == "ready"
        assert r["hub_state"] == "stale"

    def test_battery_passthrough(self):
        r = _state(battery=42)
        assert r["battery"] == 42

    def test_battery_stale_flag_propagated(self):
        r = _state(battery=50, battery_stale=True)
        assert r["battery"] == 50
        assert r["battery_stale"] is True

    def test_battery_not_stale_when_usb_ready_and_flag_false(self):
        r = _state(battery=80, battery_stale=False)
        assert r["battery_stale"] is False

    def test_no_battery(self):
        r = _state()
        assert r["battery"] is None
        assert r["battery_stale"] is False

    def test_busy_false_by_default(self):
        r = _state()
        assert r["busy"] is False


# ---------------------------------------------------------------------------
# ADB missing (highest priority)
# ---------------------------------------------------------------------------

class TestAdbMissing:
    def test_adb_missing_reason(self):
        r = _state(adb_available=False)
        assert r["reason"] == "ADB_MISSING"

    def test_adb_missing_blocks_can_check(self):
        r = _state(adb_available=False)
        assert r["can_check"] is False
        assert r["can_open_screen"] is False

    def test_adb_missing_beats_bad_usb(self):
        r = describe_phone_state(
            usb_state="unauthorized",
            hub_state="available",
            adb_available=False,
            scrcpy_available=True,
        )
        assert r["reason"] == "ADB_MISSING"


# ---------------------------------------------------------------------------
# USB state routes
# ---------------------------------------------------------------------------

class TestUsbStateRoutes:
    @pytest.mark.parametrize("usb,expected_reason", [
        ("unauthorized", "USB_AUTH_REQUIRED"),
        ("offline",      "USB_OFFLINE"),
        ("unverified",   "USB_UNVERIFIED"),
        ("ambiguous",    "USB_AMBIGUOUS"),
        ("disconnected", "USB_DISCONNECTED"),
    ])
    def test_non_ready_usb_states(self, usb, expected_reason):
        r = describe_phone_state(
            usb_state=usb,
            hub_state="available",
            adb_available=True,
            scrcpy_available=True,
        )
        assert r["reason"] == expected_reason
        assert r["can_check"] is False
        assert r["can_open_screen"] is False

    def test_non_ready_usb_makes_battery_stale(self):
        """A stored reading must be marked stale when USB is not ready."""
        r = describe_phone_state(
            usb_state="offline",
            hub_state="available",
            adb_available=True,
            scrcpy_available=True,
            battery=55,
            battery_stale=False,
        )
        assert r["battery"] == 55
        assert r["battery_stale"] is True

    def test_non_ready_usb_no_battery_not_stale(self):
        """No stored reading → battery_stale stays False even when USB is off."""
        r = describe_phone_state(
            usb_state="offline",
            hub_state="available",
            adb_available=True,
            scrcpy_available=True,
            battery=None,
            battery_stale=False,
        )
        assert r["battery"] is None
        assert r["battery_stale"] is False

    def test_different_usb_routes_reflect_separate_states(self):
        states = ["unauthorized", "offline", "unverified", "ambiguous", "disconnected"]
        reasons = []
        for usb in states:
            r = describe_phone_state(
                usb_state=usb,
                hub_state="available",
                adb_available=True,
                scrcpy_available=True,
            )
            reasons.append(r["reason"])
        # All five routes are distinct.
        assert len(set(reasons)) == len(states)


# ---------------------------------------------------------------------------
# Hub state routes
# ---------------------------------------------------------------------------

class TestHubStateRoutes:
    @pytest.mark.parametrize("hub", ["available", "stale", "unlinked", "unavailable"])
    def test_all_hub_states_accepted(self, hub):
        r = _state(hub_state=hub)
        assert r["hub_state"] == hub

    def test_different_hub_states_reflect_separate_states(self):
        hub_states = ["available", "stale", "unlinked", "unavailable"]
        seen = {_state(hub_state=h)["hub_state"] for h in hub_states}
        assert seen == set(hub_states)


# ---------------------------------------------------------------------------
# DEVICE_BUSY
# ---------------------------------------------------------------------------

class TestDeviceBusy:
    def test_busy_reason(self):
        r = _state(busy=True)
        assert r["reason"] == "DEVICE_BUSY"

    def test_busy_blocks_can_check(self):
        r = _state(busy=True)
        assert r["can_check"] is False
        assert r["can_open_screen"] is False

    def test_busy_is_propagated(self):
        r = _state(busy=True)
        assert r["busy"] is True

    def test_busy_lower_priority_than_usb(self):
        """Offline USB should win over busy flag."""
        r = describe_phone_state(
            usb_state="offline",
            hub_state="available",
            adb_available=True,
            scrcpy_available=True,
            busy=True,
        )
        assert r["reason"] == "USB_OFFLINE"


# ---------------------------------------------------------------------------
# SCRCPY missing (preserves USB check capability)
# ---------------------------------------------------------------------------

class TestScrcpyMissing:
    def test_scrcpy_missing_reason(self):
        r = _state(scrcpy_available=False)
        assert r["reason"] == "SCRCPY_MISSING"

    def test_scrcpy_missing_still_allows_can_check(self):
        """Missing scrcpy must NOT remove the ADB check ability."""
        r = _state(scrcpy_available=False)
        assert r["can_check"] is True

    def test_scrcpy_missing_blocks_can_open_screen(self):
        r = _state(scrcpy_available=False)
        assert r["can_open_screen"] is False

    def test_both_binaries_missing(self):
        """ADB missing takes priority over scrcpy missing."""
        r = describe_phone_state(
            usb_state="ready",
            hub_state="available",
            adb_available=False,
            scrcpy_available=False,
        )
        assert r["reason"] == "ADB_MISSING"


# ---------------------------------------------------------------------------
# Battery sanitisation
# ---------------------------------------------------------------------------

class TestBatterySanitisation:
    @pytest.mark.parametrize("bad_value", [True, False, -1, 101, 200, -100, "50", 3.5, None])
    def test_invalid_battery_becomes_none(self, bad_value):
        r = _state(battery=bad_value)
        assert r["battery"] is None

    @pytest.mark.parametrize("good_value", [0, 1, 50, 99, 100])
    def test_valid_battery_passthrough(self, good_value):
        r = _state(battery=good_value)
        assert r["battery"] == good_value

    def test_bool_battery_stale_flag_unaffected(self):
        """battery=True becomes None; battery_stale stays as given."""
        r = _state(battery=True, battery_stale=True)
        assert r["battery"] is None
        # No actual reading, so effective stale follows the flag only.
        assert r["battery_stale"] is True

    def test_stale_valid_data(self):
        """A valid reading with battery_stale=True is preserved as stale."""
        r = _state(battery=73, battery_stale=True)
        assert r["battery"] == 73
        assert r["battery_stale"] is True


# ---------------------------------------------------------------------------
# Unknown / invalid input rejection
# ---------------------------------------------------------------------------

class TestUnknownInputRejection:
    def test_unknown_usb_state_raises(self):
        with pytest.raises(ValueError, match="usb_state"):
            describe_phone_state(
                usb_state="charging",
                hub_state="available",
                adb_available=True,
                scrcpy_available=True,
            )

    def test_unknown_hub_state_raises(self):
        with pytest.raises(ValueError, match="hub_state"):
            describe_phone_state(
                usb_state="ready",
                hub_state="connected",
                adb_available=True,
                scrcpy_available=True,
            )

    def test_empty_usb_state_raises(self):
        with pytest.raises(ValueError):
            describe_phone_state(
                usb_state="",
                hub_state="available",
                adb_available=True,
                scrcpy_available=True,
            )

    def test_empty_hub_state_raises(self):
        with pytest.raises(ValueError):
            describe_phone_state(
                usb_state="ready",
                hub_state="",
                adb_available=True,
                scrcpy_available=True,
            )


# ---------------------------------------------------------------------------
# Output shape / contract
# ---------------------------------------------------------------------------

class TestOutputShape:
    REQUIRED_KEYS = {
        "usb_state", "hub_state", "battery", "battery_stale",
        "busy", "can_check", "can_open_screen", "reason",
    }

    def test_output_has_all_required_keys(self):
        r = _state()
        assert set(r.keys()) == self.REQUIRED_KEYS

    def test_no_extra_keys(self):
        r = _state()
        assert set(r.keys()) <= self.REQUIRED_KEYS

    def test_reason_is_stable_string(self):
        for usb in ["ready", "unauthorized", "offline", "unverified", "ambiguous", "disconnected"]:
            r = describe_phone_state(
                usb_state=usb,
                hub_state="available",
                adb_available=True,
                scrcpy_available=True,
            )
            assert isinstance(r["reason"], str)
