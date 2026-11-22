"""
Standalone macOS bridge to the DroidHub hub-helper XPC/Mach service.

This module is intentionally self-contained: it imports only the Python
standard library so it can be bundled with a macOS desktop application
without dragging in the rest of the project.

Public API
----------
MacOSHubTransportError   – raised on every failure; carries a ``kind``
MacOSHubTransport        – call list_devices_payload() to get raw bytes
"""

from __future__ import annotations

import ctypes
import pathlib
import sys
from enum import Enum

__all__ = ["MacOSHubTransport", "MacOSHubTransportError"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MACH_SERVICE_LABEL: str = "site.molokohub.droidhub.hub-helper.v1"

_REQUIREMENT_TEMPLATE: str = (
    'identifier "site.molokohub.droidhub.hub-helper.v1"'
    ' and certificate leaf = H"{hex}"'
)

_CAPACITY: int = 2 * 1024 * 1024  # 2 MiB

# C-level status codes returned by dh_hub_list_devices
_C_UNAVAILABLE: int = 1
_C_TEMPORARY: int = 2
_C_INVALID_A: int = 3
_C_INVALID_B: int = 4

# Server-level status codes embedded in the response envelope
_SRV_AUTHORIZATION: int = 1
_SRV_TEMPORARY: int = 2
_SRV_TRUST: int = 3
_SRV_INVALID: int = 4


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class ErrorKind(str, Enum):
    unavailable = "unavailable"
    temporary = "temporary"
    authorization = "authorization"
    trust = "trust"
    invalid = "invalid"


class MacOSHubTransportError(Exception):
    """Transport error with a stable *kind* that callers may branch on.

    The human-readable *message* is intentionally generic – it never
    contains raw payload data or authentication tokens.
    """

    kind: ErrorKind

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_sha1_hex(value: str) -> str:
    """Return *value* unchanged if it is a valid 40-hex SHA-1 string.

    Raises ValueError otherwise.
    """
    if len(value) != 40:
        raise ValueError(
            f"signing_cert_sha1 must be exactly 40 hex characters, got {len(value)}"
        )
    invalid = set(value) - set("0123456789abcdefABCDEF")
    if invalid:
        raise ValueError(
            f"signing_cert_sha1 contains non-hex characters: {sorted(invalid)!r}"
        )
    return value


def _make_requirement(hex_sha1: str) -> str:
    return _REQUIREMENT_TEMPLATE.format(hex=hex_sha1)


# ---------------------------------------------------------------------------
# C ABI binding
# ---------------------------------------------------------------------------


class _HubLibrary:
    """Thin ctypes wrapper around the hub-helper shared library."""

    def __init__(self, path: pathlib.Path) -> None:
        self._lib = ctypes.CDLL(str(path))
        fn = self._lib.dh_hub_list_devices
        fn.restype = ctypes.c_int
        fn.argtypes = [
            ctypes.c_char_p,  # label
            ctypes.c_char_p,  # server_req
            ctypes.c_void_p,  # out
            ctypes.c_size_t,  # capacity
            ctypes.POINTER(ctypes.c_size_t),  # written
            ctypes.POINTER(ctypes.c_int),  # server_status
        ]
        self._fn = fn

    def list_devices(
        self,
        label: str,
        server_req: str,
        capacity: int,
    ) -> bytes:
        """Call dh_hub_list_devices and return the raw payload bytes.

        Raises MacOSHubTransportError on any transport or server error.
        """
        buf = ctypes.create_string_buffer(capacity)
        written = ctypes.c_size_t(0)
        server_status = ctypes.c_int(0)

        rc = self._fn(
            label.encode(),
            server_req.encode(),
            buf,
            ctypes.c_size_t(capacity),
            ctypes.byref(written),
            ctypes.byref(server_status),
        )

        # --- C-level error handling ---
        if rc != 0:
            if rc == _C_UNAVAILABLE:
                raise MacOSHubTransportError(
                    ErrorKind.unavailable,
                    "Hub helper service is not available on this system.",
                )
            if rc == _C_TEMPORARY:
                raise MacOSHubTransportError(
                    ErrorKind.temporary,
                    "Hub helper service returned a transient error; try again.",
                )
            if rc in (_C_INVALID_A, _C_INVALID_B):
                raise MacOSHubTransportError(
                    ErrorKind.invalid,
                    "Hub helper service reported an invalid request.",
                )
            # Unknown C error – treat as transient so callers may retry
            raise MacOSHubTransportError(
                ErrorKind.temporary,
                "Hub helper service returned an unexpected error.",
            )

        # --- Server-level error handling (rc == 0 but server reported error) ---
        srv = server_status.value
        if srv != 0:
            if srv == _SRV_AUTHORIZATION:
                raise MacOSHubTransportError(
                    ErrorKind.authorization,
                    "Not authorized to access the hub helper service.",
                )
            if srv == _SRV_TEMPORARY:
                raise MacOSHubTransportError(
                    ErrorKind.temporary,
                    "Hub helper service returned a transient server error; try again.",
                )
            if srv == _SRV_TRUST:
                raise MacOSHubTransportError(
                    ErrorKind.trust,
                    "Hub helper service reported a TLS certificate trust error.",
                )
            if srv == _SRV_INVALID:
                raise MacOSHubTransportError(
                    ErrorKind.invalid,
                    "Hub helper service reported the request was invalid.",
                )
            # Unknown server error
            raise MacOSHubTransportError(
                ErrorKind.temporary,
                "Hub helper service returned an unrecognised server error.",
            )

        # --- Validate length on success ---
        n = written.value
        if n == 0 or n > capacity:
            raise MacOSHubTransportError(
                ErrorKind.invalid,
                "Hub helper service returned an unexpected payload length.",
            )

        return bytes(buf.raw[:n])


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class MacOSHubTransport:
    """Bridge to the macOS hub-helper Mach service via a bundled C library.

    Parameters
    ----------
    library_path:
        Absolute path to the compiled hub-helper shared library
        (``libdroidhub-helper.dylib`` or similar).  The caller (packaging
        code) is responsible for locating it; this class does *not* search
        ``sys.path`` or ``DYLD_LIBRARY_PATH``.
    signing_cert_sha1:
        40-character lower- or upper-case hex SHA-1 fingerprint of the
        leaf certificate used to sign the hub-helper process.  Used to
        construct the Mach code-signing requirement.

    Raises
    ------
    ValueError
        If *signing_cert_sha1* is not a valid 40-hex string.
    MacOSHubTransportError(kind='unavailable')
        On non-macOS platforms the first call to *list_devices_payload*
        raises this error; construction itself does not.
    """

    def __init__(self, library_path: pathlib.Path, signing_cert_sha1: str) -> None:
        if not pathlib.Path(library_path).is_absolute():
            raise ValueError("library_path must be an absolute path")
        self._library_path: pathlib.Path = library_path
        self._sha1: str = _validate_sha1_hex(signing_cert_sha1)
        self._requirement: str = _make_requirement(self._sha1)
        # Lazy – we do NOT load the library or ctypes on construction so
        # that import-time usage on non-macOS doesn't fail.
        self._lib: _HubLibrary | None = None

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _get_lib(self) -> _HubLibrary:
        """Return the (lazily loaded) C library wrapper."""
        if sys.platform != "darwin":
            raise MacOSHubTransportError(
                ErrorKind.unavailable,
                "MacOSHubTransport is only supported on macOS.",
            )
        if self._lib is None:
            try:
                self._lib = _HubLibrary(self._library_path)
            except OSError:
                raise MacOSHubTransportError(
                    ErrorKind.unavailable,
                    "Hub helper library could not be loaded.",
                ) from None
            except AttributeError:
                raise MacOSHubTransportError(
                    ErrorKind.unavailable,
                    "Hub helper library is missing a required symbol.",
                ) from None
        return self._lib

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def list_devices_payload(self) -> bytes:
        """Return the raw bytes payload from the hub-helper list-devices call.

        The payload is an opaque blob; callers must not log or store its
        contents in persistent storage.

        Raises
        ------
        MacOSHubTransportError
            On any transport or server error.
        """
        lib = self._get_lib()
        return lib.list_devices(
            _MACH_SERVICE_LABEL,
            self._requirement,
            _CAPACITY,
        )
