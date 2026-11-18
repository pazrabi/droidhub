"""
Standalone local USB device alias store for DroidHub Desktop.

Stores a JSON file (schema version 1) mapping serial numbers to display aliases.
Thread/process safe via advisory file locking (fcntl on POSIX, msvcrt on Windows).
Atomic replace through a sibling tempfile.  No third-party dependencies.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class DeviceAliasError(Exception):
    """Raised for file I/O errors, corrupt data, or unsupported schema."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_MAX_ALIAS_LEN = 60
_SCHEMA_VERSION = 1


def _is_control_or_nonprintable(ch: str) -> bool:
    """Return True if the character is a control char or non-printable."""
    cat = unicodedata.category(ch)
    # Cc = control, Cs = surrogate, Cn = unassigned, Co = private-use
    if cat in ("Cc", "Cs", "Cn"):
        return True
    # Also reject anything that is not printable per Python's str.isprintable
    return not ch.isprintable()


def _validate_alias(alias: str) -> str:
    """Trim, validate, and return the alias.  Raise ValueError on failure."""
    trimmed = alias.strip()
    if not trimmed:
        raise ValueError("alias is blank after trimming")
    if len(trimmed) > _MAX_ALIAS_LEN:
        raise ValueError(f"alias length {len(trimmed)} exceeds maximum {_MAX_ALIAS_LEN}")
    for ch in trimmed:
        if _is_control_or_nonprintable(ch):
            raise ValueError(f"alias contains control/non-printable character {ch!r}")
    return trimmed


def _validate_serial(serial: str) -> str:
    """Validate a device serial.  Raise ValueError on failure."""
    if not serial or not serial.strip():
        raise ValueError("serial must be non-blank")
    # Reject newline characters
    if "\n" in serial or "\r" in serial:
        raise ValueError("serial must not contain newline characters")
    # Reject host:port style (TCP/IP ADB addresses like 192.168.1.1:5555)
    if ":" in serial:
        raise ValueError("serial must not be a host:port address")
    # Reject emulator-NNNN style serials
    if serial.startswith("emulator-"):
        raise ValueError("serial must not be an emulator serial")
    # Every character must be printable
    for ch in serial:
        if not ch.isprintable():
            raise ValueError(f"serial contains non-printable character {ch!r}")
    return serial


# ---------------------------------------------------------------------------
# File locking (stdlib only)
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover – tested on Windows CI
    import msvcrt

    def _lock_file(fh) -> None:  # type: ignore[type-arg]
        # Lock the first byte (advisory exclusive lock via LockFileEx semantics)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(fh) -> None:  # type: ignore[type-arg]
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _lock_file(fh) -> None:  # type: ignore[type-arg]
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock_file(fh) -> None:  # type: ignore[type-arg]
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# JSON persistence helpers
# ---------------------------------------------------------------------------

def _load_data(path: Path) -> dict[str, str]:
    """
    Read and parse the alias store file.

    Returns an empty dict if the file does not exist.
    Raises DeviceAliasError for any other I/O or parse problem.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DeviceAliasError(f"Cannot read alias store {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeviceAliasError(f"Alias store {path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise DeviceAliasError(f"Alias store {path} has unexpected top-level type")

    version = data.get("version")
    if version != _SCHEMA_VERSION:
        raise DeviceAliasError(
            f"Alias store {path} has unsupported schema version: {version!r}"
        )

    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        raise DeviceAliasError(f"Alias store {path} 'aliases' field is not a dict")

    # Ensure all keys and values are strings
    for k, v in aliases.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise DeviceAliasError(
                f"Alias store {path} contains non-string key/value: {k!r}={v!r}"
            )

    return dict(aliases)


def _save_data(path: Path, aliases: dict[str, str]) -> None:
    """
    Atomically write aliases to *path* using a sibling tempfile.

    Raises DeviceAliasError on any I/O failure (tempfile is cleaned up).
    """
    payload = {"version": _SCHEMA_VERSION, "aliases": aliases}
    serialised = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    parent = path.parent
    tmp_path: Optional[Path] = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            prefix=".aliases_tmp_",
            suffix=".json",
            dir=parent,
        )
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialised)
        except Exception:
            # fd already owned by fdopen; if fdopen itself failed fd leaks but
            # the temp file still needs removing.
            os.close(fd) if os.path.exists(tmp_str) else None  # type: ignore[func-returns-value]
            raise
        # Atomic replace
        tmp_path.replace(path)
        tmp_path = None  # ownership transferred
    except DeviceAliasError:
        raise
    except OSError as exc:
        raise DeviceAliasError(f"Cannot write alias store {path}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DeviceAliasStore:
    """
    Persistent alias store for local USB Android devices.

    Parameters
    ----------
    path:
        Path to the JSON file used for storage.  The file is created on the
        first call to :meth:`set_alias`; a missing file is treated as empty.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def list_aliases(self) -> dict[str, str]:
        """
        Return a snapshot of all aliases as ``{serial: alias}``.

        Missing file → empty dict.
        Corrupt/inaccessible file → raises :class:`DeviceAliasError`.
        """
        return _load_data(self._path)

    def set_alias(self, serial: str, alias: str | None) -> None:
        """
        Set or clear the alias for *serial*.

        Parameters
        ----------
        serial:
            Exact device serial (non-blank, printable, no host:port, no
            emulator-NNNN, no newline).
        alias:
            Display name to associate with the serial, or ``None`` to remove
            the alias.  When a string is supplied it is trimmed; blank-after-
            trim, control/non-printable characters, and lengths > 60 are
            rejected with :class:`ValueError`.

        Raises
        ------
        ValueError
            If *serial* or *alias* fails validation.
        DeviceAliasError
            If the store file cannot be read or written.
        """
        # Validate inputs before touching the file
        serial = _validate_serial(serial)
        trimmed_alias: Optional[str] = None
        if alias is not None:
            trimmed_alias = _validate_alias(alias)

        # Locked read-modify-write
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            lock_fh = open(lock_path, "a", encoding="utf-8")  # noqa: WPS515
        except OSError as exc:
            raise DeviceAliasError(
                f"Cannot open lock file {lock_path}: {exc}"
            ) from exc

        try:
            _lock_file(lock_fh)
            aliases = _load_data(self._path)
            if trimmed_alias is None:
                aliases.pop(serial, None)
            else:
                aliases[serial] = trimmed_alias
            _save_data(self._path, aliases)
        finally:
            _unlock_file(lock_fh)
            lock_fh.close()
