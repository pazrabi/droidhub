"""
Tests for desktop/macos_hub_transport.py

These tests are designed to run on both Linux and macOS using
injected ctypes doubles via monkeypatch; no real macOS library is needed.
"""

from __future__ import annotations

import ctypes
import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
from desktop.macos_hub_transport import (
    ErrorKind,
    MacOSHubTransport,
    MacOSHubTransportError,
    _CAPACITY,
    _MACH_SERVICE_LABEL,
    _make_requirement,
    _validate_sha1_hex,
)

# ---------------------------------------------------------------------------
# Test fixtures & helpers
# ---------------------------------------------------------------------------

VALID_SHA1 = "a" * 40  # 40 lowercase hex chars
VALID_SHA1_UPPER = "A" * 40  # 40 uppercase hex chars
LIBRARY_PATH = pathlib.Path("/fake/libdroidhub-helper.dylib")


def _make_transport(sha1: str = VALID_SHA1) -> MacOSHubTransport:
    return MacOSHubTransport(LIBRARY_PATH, sha1)


class _FakeCDLL:
    """Minimal stand-in for ctypes.CDLL that lets tests control return values."""

    def __init__(
        self,
        rc: int = 0,
        server_status: int = 0,
        payload: bytes = b'{"devices":[]}',
        written_override: int | None = None,
    ) -> None:
        self._rc = rc
        self._server_status = server_status
        self._payload = payload
        self._written_override = written_override

        # Mimic attribute access for the dh_hub_list_devices function
        self.dh_hub_list_devices = MagicMock(side_effect=self._call)
        # restype / argtypes are set externally
        self.dh_hub_list_devices.restype = None
        self.dh_hub_list_devices.argtypes = None

        # Track last call arguments for assertions
        self.last_label: bytes | None = None
        self.last_server_req: bytes | None = None

    def _call(
        self,
        label: bytes,
        server_req: bytes,
        buf: Any,
        capacity: ctypes.c_size_t,
        written_ptr: Any,
        server_status_ptr: Any,
    ) -> int:
        self.last_label = label
        self.last_server_req = server_req

        # Write payload into buffer
        payload = self._payload
        if isinstance(buf, ctypes.Array):
            n = min(len(payload), len(buf))
            ctypes.memmove(buf, payload, n)

        # Set written
        written = self._written_override if self._written_override is not None else len(payload)
        ctypes.cast(written_ptr, ctypes.POINTER(ctypes.c_size_t))[0] = written

        # Set server_status
        ctypes.cast(server_status_ptr, ctypes.POINTER(ctypes.c_int))[0] = self._server_status

        return self._rc


def _inject_fake_lib(monkeypatch: pytest.MonkeyPatch, fake_cdll: _FakeCDLL) -> None:
    """Monkeypatch ctypes.CDLL so _HubLibrary uses our fake."""
    monkeypatch.setattr(ctypes, "CDLL", lambda path: fake_cdll)


def _force_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sys.platform appear as darwin so non-macOS CI can run tests."""
    monkeypatch.setattr(sys, "platform", "darwin")


# ===========================================================================
# SHA-1 validation
# ===========================================================================


class TestValidateSha1Hex:
    def test_accepts_40_lowercase_hex(self) -> None:
        result = _validate_sha1_hex("deadbeef" + "a" * 32)
        assert len(result) == 40

    def test_accepts_40_uppercase_hex(self) -> None:
        result = _validate_sha1_hex("DEADBEEF" + "A" * 32)
        assert len(result) == 40

    def test_accepts_mixed_case(self) -> None:
        result = _validate_sha1_hex("DeAdBeEf" + "a" * 32)
        assert len(result) == 40

    def test_rejects_too_short(self) -> None:
        with pytest.raises(ValueError, match="40 hex"):
            _validate_sha1_hex("a" * 39)

    def test_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="40 hex"):
            _validate_sha1_hex("a" * 41)

    def test_rejects_non_hex_chars(self) -> None:
        with pytest.raises(ValueError, match="non-hex"):
            _validate_sha1_hex("g" + "a" * 39)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            _validate_sha1_hex("")


# ===========================================================================
# Requirement string construction
# ===========================================================================


class TestMakeRequirement:
    def test_exact_pin_string(self) -> None:
        sha1 = "a" * 40
        req = _make_requirement(sha1)
        expected = (
            f'identifier "site.molokohub.droidhub.hub-helper.v1"'
            f' and certificate leaf = H"{sha1}"'
        )
        assert req == expected

    def test_contains_full_sha1(self) -> None:
        sha1 = "deadbeef" + "0" * 32
        req = _make_requirement(sha1)
        assert sha1 in req

    def test_identifier_locked(self) -> None:
        req = _make_requirement(VALID_SHA1)
        assert 'identifier "site.molokohub.droidhub.hub-helper.v1"' in req

    def test_cert_leaf_format(self) -> None:
        req = _make_requirement(VALID_SHA1)
        assert f'certificate leaf = H"{VALID_SHA1}"' in req


# ===========================================================================
# Construction
# ===========================================================================


class TestMacOSHubTransportConstruction:
    def test_valid_construction(self) -> None:
        t = _make_transport()
        assert t is not None

    def test_construction_accepts_uppercase_sha1(self) -> None:
        t = _make_transport(VALID_SHA1_UPPER)
        assert t is not None

    def test_construction_rejects_bad_sha1(self) -> None:
        with pytest.raises(ValueError):
            MacOSHubTransport(LIBRARY_PATH, "not-a-sha1")

    def test_construction_does_not_load_ctypes_on_non_macos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing on Linux must not raise – rejection is lazy."""
        monkeypatch.setattr(sys, "platform", "linux")
        t = _make_transport()
        assert t is not None


# ===========================================================================
# Non-macOS rejection (lazy)
# ===========================================================================


class TestNonMacOSRejection:
    def test_raises_unavailable_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        assert exc_info.value.kind == ErrorKind.unavailable

    def test_raises_unavailable_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        assert exc_info.value.kind == ErrorKind.unavailable

    def test_unavailable_message_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        # Message must not contain any secret/token material
        msg = str(exc_info.value)
        assert VALID_SHA1 not in msg


# ===========================================================================
# Mach service label is pinned
# ===========================================================================


class TestMachServiceLabel:
    def test_label_is_fixed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=0, server_status=0, payload=b"ok_payload")
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        t.list_devices_payload()
        assert fake.last_label == _MACH_SERVICE_LABEL.encode()

    def test_label_cannot_be_overridden_at_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """There is no public API to change the label – verify it is always the constant."""
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=0, server_status=0, payload=b"ok_payload")
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        t.list_devices_payload()
        assert fake.last_label == b"site.molokohub.droidhub.hub-helper.v1"


# ===========================================================================
# Signing requirement pin
# ===========================================================================


class TestSigningRequirementPin:
    def test_exact_requirement_string_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_darwin(monkeypatch)
        sha1 = "deadbeef" + "0" * 32
        fake = _FakeCDLL(rc=0, server_status=0, payload=b"ok_payload")
        _inject_fake_lib(monkeypatch, fake)
        t = MacOSHubTransport(LIBRARY_PATH, sha1)
        t.list_devices_payload()
        expected_req = (
            f'identifier "site.molokohub.droidhub.hub-helper.v1"'
            f' and certificate leaf = H"{sha1}"'
        ).encode()
        assert fake.last_server_req == expected_req

    def test_different_sha1_gives_different_requirement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_darwin(monkeypatch)
        sha1_a = "aaaa" + "0" * 36
        sha1_b = "bbbb" + "0" * 36
        reqs: list[bytes] = []

        for sha1 in (sha1_a, sha1_b):
            fake = _FakeCDLL(rc=0, server_status=0, payload=b"ok_payload")
            _inject_fake_lib(monkeypatch, fake)
            t = MacOSHubTransport(LIBRARY_PATH, sha1)
            t.list_devices_payload()
            reqs.append(fake.last_server_req)  # type: ignore[arg-type]

        assert reqs[0] != reqs[1]
        assert sha1_a.encode() in reqs[0]
        assert sha1_b.encode() in reqs[1]


# ===========================================================================
# C transport status codes
# ===========================================================================


class TestCTransportStatusCodes:
    def _call_with_rc(
        self, monkeypatch: pytest.MonkeyPatch, rc: int
    ) -> MacOSHubTransportError:
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=rc)
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        return exc_info.value

    def test_c_rc_1_gives_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_rc(monkeypatch, 1)
        assert err.kind == ErrorKind.unavailable

    def test_c_rc_2_gives_temporary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_rc(monkeypatch, 2)
        assert err.kind == ErrorKind.temporary

    def test_c_rc_3_gives_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_rc(monkeypatch, 3)
        assert err.kind == ErrorKind.invalid

    def test_c_rc_4_gives_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_rc(monkeypatch, 4)
        assert err.kind == ErrorKind.invalid


# ===========================================================================
# Server-level status codes
# ===========================================================================


class TestServerStatusCodes:
    def _call_with_srv(
        self, monkeypatch: pytest.MonkeyPatch, srv: int
    ) -> MacOSHubTransportError:
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=0, server_status=srv, payload=b"payload")
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        return exc_info.value

    def test_srv_1_gives_authorization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_srv(monkeypatch, 1)
        assert err.kind == ErrorKind.authorization

    def test_srv_2_gives_temporary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_srv(monkeypatch, 2)
        assert err.kind == ErrorKind.temporary

    def test_srv_3_gives_trust(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_srv(monkeypatch, 3)
        assert err.kind == ErrorKind.trust

    def test_srv_4_gives_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = self._call_with_srv(monkeypatch, 4)
        assert err.kind == ErrorKind.invalid


# ===========================================================================
# Successful transport – payload handling
# ===========================================================================


class TestSuccessfulTransport:
    def test_returns_raw_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_darwin(monkeypatch)
        payload = b'{"devices": ["serial1", "serial2"]}'
        fake = _FakeCDLL(rc=0, server_status=0, payload=payload)
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        result = t.list_devices_payload()
        assert result == payload

    def test_capacity_passed_to_c_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=0, server_status=0, payload=b"data")
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        t.list_devices_payload()
        # Verify that the C function was called (via the mock side_effect)
        assert fake.dh_hub_list_devices.called

    def test_capacity_is_2mib(self) -> None:
        assert _CAPACITY == 2 * 1024 * 1024


# ===========================================================================
# Invalid payload length on success
# ===========================================================================


class TestInvalidSuccessLength:
    def test_zero_written_raises_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_darwin(monkeypatch)
        # written_override=0 simulates "rc=0 but no bytes written"
        fake = _FakeCDLL(rc=0, server_status=0, payload=b"data", written_override=0)
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        assert exc_info.value.kind == ErrorKind.invalid

    def test_written_exceeds_capacity_raises_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_darwin(monkeypatch)
        # written_override > _CAPACITY simulates a rogue/buggy C lib
        fake = _FakeCDLL(
            rc=0,
            server_status=0,
            payload=b"data",
            written_override=_CAPACITY + 1,
        )
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        assert exc_info.value.kind == ErrorKind.invalid


# ===========================================================================
# Error safety – messages must not leak raw payload or tokens
# ===========================================================================


class TestErrorMessageSafety:
    """Error messages must be generic and not contain raw payload data."""

    def _get_error_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rc: int = 0,
        srv: int = 0,
        payload: bytes = b"SECRET_TOKEN_DATA_12345",
    ) -> str:
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=rc, server_status=srv, payload=payload)
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        return str(exc_info.value)

    def test_srv_error_msg_has_no_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = self._get_error_message(monkeypatch, rc=0, srv=1)
        assert b"SECRET_TOKEN_DATA_12345".decode() not in msg

    def test_c_error_msg_has_no_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = self._get_error_message(monkeypatch, rc=1)
        assert b"SECRET_TOKEN_DATA_12345".decode() not in msg

    def test_error_kind_is_enum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_darwin(monkeypatch)
        fake = _FakeCDLL(rc=1)
        _inject_fake_lib(monkeypatch, fake)
        t = _make_transport()
        with pytest.raises(MacOSHubTransportError) as exc_info:
            t.list_devices_payload()
        assert isinstance(exc_info.value.kind, ErrorKind)


# ===========================================================================
# MacOSHubTransportError kind stability
# ===========================================================================


class TestErrorKindValues:
    """Verify the string values of ErrorKind are stable (part of public ABI)."""

    def test_unavailable_value(self) -> None:
        assert ErrorKind.unavailable.value == "unavailable"

    def test_temporary_value(self) -> None:
        assert ErrorKind.temporary.value == "temporary"

    def test_authorization_value(self) -> None:
        assert ErrorKind.authorization.value == "authorization"

    def test_trust_value(self) -> None:
        assert ErrorKind.trust.value == "trust"

    def test_invalid_value(self) -> None:
        assert ErrorKind.invalid.value == "invalid"
