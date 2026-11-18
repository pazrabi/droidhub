"""
tests/test_desktop_usb_setup_status.py

Tests for desktop.usb_setup_status.assess_usb_setup.
Uses only stdlib (dataclasses) – no third-party deps.
"""

from __future__ import annotations

import sys
import os

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dataclasses
from typing import Optional

import pytest

from desktop.usb_setup_status import assess_usb_setup


# ---------------------------------------------------------------------------
# Fake DeviceInfo dataclass (matches the documented shape)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class FakeDevice:
    serial: str
    state: str
    is_network: bool = False
    usb_info: Optional[str] = None
    is_ready: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_keys():
    return {"status", "ready_count", "unauthorized_count", "offline_count",
            "ambiguous_count", "unverified_count", "other_count"}


def call(**kwargs):
    """Convenience wrapper with default keyword args."""
    return assess_usb_setup(**kwargs)


# ---------------------------------------------------------------------------
# Gate tests – adb_missing / checking / adb_error
# ---------------------------------------------------------------------------

class TestGates:
    def test_adb_missing_returns_zero_counts(self):
        result = call(adb_available=False, inventory_state="ok", devices=[])
        assert result["status"] == "adb_missing"
        assert all(result[k] == 0 for k in result if k != "status")

    def test_adb_missing_ignores_devices(self):
        """Even with valid devices, adb_missing wins."""
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=False, inventory_state="ok", devices=[dev])
        assert result["status"] == "adb_missing"

    def test_checking_when_pending(self):
        result = call(adb_available=True, inventory_state="pending", devices=[])
        assert result["status"] == "checking"
        assert all(result[k] == 0 for k in result if k != "status")

    def test_checking_ignores_devices(self):
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="pending", devices=[dev])
        assert result["status"] == "checking"

    def test_adb_error_when_error_state(self):
        result = call(adb_available=True, inventory_state="error", devices=[])
        assert result["status"] == "adb_error"
        assert all(result[k] == 0 for k in result if k != "status")

    def test_adb_error_ignores_devices(self):
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="error", devices=[dev])
        assert result["status"] == "adb_error"

    def test_adb_missing_beats_pending(self):
        """adb_missing has higher priority than checking."""
        result = call(adb_available=False, inventory_state="pending", devices=[])
        assert result["status"] == "adb_missing"

    def test_adb_missing_beats_error(self):
        result = call(adb_available=False, inventory_state="error", devices=[])
        assert result["status"] == "adb_missing"


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_all_keys_present(self):
        result = call(adb_available=True, inventory_state="ok", devices=[])
        assert set(result.keys()) == _result_keys()

    def test_no_raw_serial_in_output(self):
        dev = FakeDevice(serial="SECRETSERIAL", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        for v in result.values():
            assert "SECRETSERIAL" not in str(v)

    def test_invalid_inventory_state_raises(self):
        with pytest.raises(ValueError):
            call(adb_available=True, inventory_state="unknown", devices=[])


# ---------------------------------------------------------------------------
# Empty / filtering
# ---------------------------------------------------------------------------

class TestFiltering:
    def test_empty_device_list(self):
        result = call(adb_available=True, inventory_state="ok", devices=[])
        assert result["status"] == "empty"

    def test_emulator_excluded(self):
        dev = FakeDevice(serial="emulator-5554", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "empty"

    def test_emulator_mixed_case_excluded(self):
        dev = FakeDevice(serial="Emulator-5554", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "empty"

    def test_network_flag_excluded(self):
        dev = FakeDevice(serial="ABC123", state="device", is_network=True, is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "empty"

    def test_host_port_serial_excluded(self):
        dev = FakeDevice(serial="192.168.1.5:5555", state="device", is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "empty"

    def test_network_alias_does_not_qualify_as_ready(self):
        """A network-transport serial for the same device must not count as ready."""
        local = FakeDevice(serial="ABC123", state="device", is_network=False,
                           is_ready=False, usb_info=None)
        network = FakeDevice(serial="192.168.1.1:5555", state="device",
                             is_network=True, is_ready=True, usb_info="usb1")
        result = call(adb_available=True, inventory_state="ok", devices=[local, network])
        assert result["ready_count"] == 0
        assert result["status"] != "ready"

    def test_mix_of_emulator_and_network_gives_empty(self):
        devs = [
            FakeDevice(serial="emulator-5554", state="device", is_ready=True, usb_info="usb1"),
            FakeDevice(serial="10.0.0.1:5555", state="device", is_ready=True, usb_info="usb1"),
            FakeDevice(serial="USB123", state="device", is_network=True, is_ready=True, usb_info="usb1"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# Single device states
# ---------------------------------------------------------------------------

class TestSingleDeviceStates:
    def test_ready_with_usb_evidence(self):
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info="1-2.3")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "ready"
        assert result["ready_count"] == 1
        assert result["unverified_count"] == 0

    def test_device_state_no_usb_info_is_unverified(self):
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info=None)
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unverified"
        assert result["unverified_count"] == 1
        assert result["ready_count"] == 0

    def test_device_state_empty_usb_info_is_unverified(self):
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info="")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unverified"
        assert result["unverified_count"] == 1

    def test_device_state_whitespace_usb_info_is_unverified(self):
        dev = FakeDevice(serial="ABC123", state="device", is_ready=True, usb_info="   ")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unverified"
        assert result["unverified_count"] == 1

    def test_device_state_not_ready_with_usb_info_is_other(self):
        """is_ready=False but has usb_info – unusual; counts as other."""
        dev = FakeDevice(serial="ABC123", state="device", is_ready=False, usb_info="1-2")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["other_count"] == 1

    def test_unauthorized(self):
        dev = FakeDevice(serial="ABC123", state="unauthorized")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unauthorized"
        assert result["unauthorized_count"] == 1

    def test_unauthorized_no_usb_info_still_unauthorized(self):
        """No USB evidence claimed for unauthorized – that's fine."""
        dev = FakeDevice(serial="ABC123", state="unauthorized", usb_info=None)
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unauthorized"
        assert result["unauthorized_count"] == 1

    def test_offline(self):
        dev = FakeDevice(serial="ABC123", state="offline")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "offline"
        assert result["offline_count"] == 1

    def test_offline_no_usb_info_still_offline(self):
        dev = FakeDevice(serial="ABC123", state="offline", usb_info=None)
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "offline"

    def test_other_state(self):
        dev = FakeDevice(serial="ABC123", state="recovery")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unavailable"
        assert result["other_count"] == 1

    def test_bootloader_state(self):
        dev = FakeDevice(serial="ABC123", state="bootloader")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "unavailable"
        assert result["other_count"] == 1

    def test_empty_state_string(self):
        dev = FakeDevice(serial="ABC123", state="")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["other_count"] == 1


# ---------------------------------------------------------------------------
# Duplicate / ambiguous serials
# ---------------------------------------------------------------------------

class TestAmbiguous:
    def test_duplicate_serial_counts_ambiguous(self):
        devs = [
            FakeDevice(serial="DUPE", state="device", is_ready=True, usb_info="1-2"),
            FakeDevice(serial="DUPE", state="device", is_ready=True, usb_info="1-3"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["ambiguous_count"] == 2
        assert result["ready_count"] == 0

    def test_missing_serial_counts_ambiguous(self):
        dev = FakeDevice(serial="", state="device", is_ready=True, usb_info="1-2")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["ambiguous_count"] == 1
        assert result["ready_count"] == 0

    def test_whitespace_serial_counts_ambiguous(self):
        dev = FakeDevice(serial="   ", state="device", is_ready=True, usb_info="1-2")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["ambiguous_count"] == 1

    def test_ambiguous_status_set(self):
        devs = [
            FakeDevice(serial="DUPE", state="device", is_ready=True, usb_info="1-2"),
            FakeDevice(serial="DUPE", state="device", is_ready=True, usb_info="1-3"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "ambiguous"

    def test_one_good_one_duplicate(self):
        """One valid device + one duplicate pair -> ambiguous_count=2, ready_count=1,
        status=unauthorized if no unauth, but let's check the counts."""
        devs = [
            FakeDevice(serial="GOOD1", state="device", is_ready=True, usb_info="1-1"),
            FakeDevice(serial="DUPE", state="device", is_ready=True, usb_info="1-2"),
            FakeDevice(serial="DUPE", state="device", is_ready=True, usb_info="1-3"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["ready_count"] == 1
        assert result["ambiguous_count"] == 2
        # With both ready and ambiguous, priority goes to ambiguous (higher in table)
        assert result["status"] == "ambiguous"


# ---------------------------------------------------------------------------
# Mixed / priority tests
# ---------------------------------------------------------------------------

class TestMixedStates:
    def test_unauthorized_beats_offline(self):
        devs = [
            FakeDevice(serial="DEV1", state="unauthorized"),
            FakeDevice(serial="DEV2", state="offline"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "unauthorized"
        assert result["unauthorized_count"] == 1
        assert result["offline_count"] == 1

    def test_unauthorized_beats_ready(self):
        devs = [
            FakeDevice(serial="DEV1", state="unauthorized"),
            FakeDevice(serial="DEV2", state="device", is_ready=True, usb_info="1-1"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "unauthorized"
        assert result["ready_count"] == 1
        assert result["unauthorized_count"] == 1

    def test_offline_beats_unverified(self):
        devs = [
            FakeDevice(serial="DEV1", state="offline"),
            FakeDevice(serial="DEV2", state="device", is_ready=True, usb_info=None),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "offline"
        assert result["offline_count"] == 1
        assert result["unverified_count"] == 1

    def test_ready_with_issues_mixed_auth_ready(self):
        """When ready + unauthorized both present, unauthorized wins."""
        devs = [
            FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="1-1"),
            FakeDevice(serial="DEV2", state="unauthorized"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        # Unauthorized has higher priority than ready_with_issues
        assert result["status"] == "unauthorized"
        assert result["ready_count"] == 1

    def test_ready_with_issues_unverified_plus_ready(self):
        """ready + unverified → ready_with_issues is NOT triggered because
        unverified has higher priority than ready_with_issues.
        Wait – per spec, priority table shows:
          8. unverified
         10. ready_with_issues
         11. ready
        So unverified beats ready_with_issues."""
        devs = [
            FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="1-1"),
            FakeDevice(serial="DEV2", state="device", is_ready=True, usb_info=None),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        # unverified (priority 8) beats ready_with_issues (priority 10)
        assert result["status"] == "unverified"
        assert result["ready_count"] == 1
        assert result["unverified_count"] == 1

    def test_ready_with_issues_other_plus_ready(self):
        """ready + other(recovery) → ready_with_issues
        because other/unavailable < ready_with_issues priority (other=unavailable at 9,
        ready_with_issues at 10 means ready_with_issues actually lower... re-read spec.

        Priority: unauthorized>offline>ambiguous>unverified>unavailable>ready_with_issues>ready
        So 'unavailable' (position 9) beats 'ready_with_issues' (position 10).
        That means: if we have ready + other-state, the other-state alone → unavailable,
        but combined with ready → ready_with_issues.
        The priority table in the docstring describes the final status pick, not individual device rank.
        """
        devs = [
            FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="1-1"),
            FakeDevice(serial="DEV2", state="recovery"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["ready_count"] == 1
        assert result["other_count"] == 1
        # ready + other → ready_with_issues (mixed good/bad)
        assert result["status"] == "ready_with_issues"

    def test_all_ready(self):
        devs = [
            FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="1-1"),
            FakeDevice(serial="DEV2", state="device", is_ready=True, usb_info="1-2"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "ready"
        assert result["ready_count"] == 2

    def test_disconnected_device(self):
        """A device in 'offline' state represents a disconnected USB device."""
        dev = FakeDevice(serial="DISC1", state="offline", usb_info=None)
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["status"] == "offline"
        assert result["offline_count"] == 1

    def test_network_only_all_excluded(self):
        """Network-only inventory → empty."""
        devs = [
            FakeDevice(serial="192.168.0.1:5555", state="device", is_ready=True, usb_info="usb"),
            FakeDevice(serial="DEV_NET", state="device", is_network=True, is_ready=True, usb_info="usb"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["status"] == "empty"
        assert result["ready_count"] == 0

    def test_usb_evidence_required_for_ready(self):
        """state==device + is_ready=True but usb_info=None → NOT ready."""
        dev = FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info=None)
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["ready_count"] == 0
        assert result["unverified_count"] == 1

    def test_three_device_mix_auth_ready(self):
        """Mixed: authorized+ready, unauthorized, offline."""
        devs = [
            FakeDevice(serial="READY1", state="device", is_ready=True, usb_info="2-1"),
            FakeDevice(serial="UNAUTH1", state="unauthorized"),
            FakeDevice(serial="OFFLINE1", state="offline"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["ready_count"] == 1
        assert result["unauthorized_count"] == 1
        assert result["offline_count"] == 1
        # unauthorized has priority
        assert result["status"] == "unauthorized"

    def test_state_case_insensitive(self):
        """State matching should be case-insensitive."""
        devs = [
            FakeDevice(serial="DEV1", state="Unauthorized"),
            FakeDevice(serial="DEV2", state="OFFLINE"),
        ]
        result = call(adb_available=True, inventory_state="ok", devices=devs)
        assert result["unauthorized_count"] == 1
        assert result["offline_count"] == 1
        assert result["status"] == "unauthorized"


# ---------------------------------------------------------------------------
# USB evidence edge cases
# ---------------------------------------------------------------------------

class TestUsbEvidence:
    def test_whitespace_usb_info_not_evidence(self):
        dev = FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="  \t  ")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["unverified_count"] == 1
        assert result["ready_count"] == 0

    def test_nonempty_usb_info_counts_as_evidence(self):
        dev = FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="usb:1-2.3")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["ready_count"] == 1
        assert result["unverified_count"] == 0

    def test_usb_info_zero_string_is_valid_evidence(self):
        """'0' is non-empty and not whitespace → counts as USB evidence."""
        dev = FakeDevice(serial="DEV1", state="device", is_ready=True, usb_info="0")
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["ready_count"] == 1


# ---------------------------------------------------------------------------
# Dict-based device input (duck-typing)
# ---------------------------------------------------------------------------

class TestDictInput:
    def test_dict_device_works(self):
        dev = {
            "serial": "DICTDEV1",
            "state": "device",
            "is_network": False,
            "usb_info": "1-1",
            "is_ready": True,
        }
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["ready_count"] == 1
        assert result["status"] == "ready"

    def test_dict_missing_optional_fields(self):
        """Dict missing optional fields should use defaults gracefully."""
        dev = {"serial": "DICTDEV2", "state": "offline"}
        result = call(adb_available=True, inventory_state="ok", devices=[dev])
        assert result["offline_count"] == 1
        assert result["status"] == "offline"
