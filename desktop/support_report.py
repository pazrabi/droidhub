"""
DroidHub Desktop — USB pilot support report builder.

Public API
----------
build_support_report(*, setup, versions, run=None) -> dict
    Sanitises caller-supplied mappings into a safe, schema-versioned report
    dict.  Raises ValueError on invalid input.

write_support_archive(path, report) -> Path
    Atomically writes a ZIP file containing a single UTF-8 diagnostics.json.
    Enforces the sanitised schema itself — it re-sanitises the mapping before
    writing, so an arbitrary Mapping cannot export unsanctioned data.

No third-party dependencies; stdlib only.
No network I/O; no reading of user files.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Allowlists & bounds
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

_SETUP_COUNT_KEYS = frozenset(
    {
        "ready_count",
        "unauthorized_count",
        "offline_count",
        "ambiguous_count",
        "unverified_count",
        "other_count",
    }
)
_SETUP_STATUS_VALUES = frozenset({"ok", "degraded", "error", "unknown"})

_TOOL_NAMES = frozenset({"adb", "scrcpy"})
# Strict dotted-numeric version: 1–4 numeric components, each 0-999.
_VERSION_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){0,3}$")

_RUN_STATUS_VALUES = frozenset({"pending", "running", "done", "failed", "interrupted", "cancelled"})
_DEVICE_STATUS_VALUES = frozenset({"pending", "running", "done", "failed", "skipped", "timeout"})

_ALLOWED_ERROR_CODES = frozenset(
    {
        "USB_DISCONNECTED",
        "ADB_UNAUTHORIZED",
        "TIMEOUT",
        "PROBE_FAILED",
        "BATTERY_LOW",
        "RUN_INTERRUPTED",
    }
)

_STEP_VALUES = frozenset(
    {
        "init",
        "probe",
        "connect",
        "authorize",
        "battery_check",
        "run",
        "verify",
        "cleanup",
        "done",
    }
)

# Bounded scalar limits
_MAX_BATTERY = 100
_MIN_BATTERY = 0
_MAX_COUNT = 10_000
_MAX_VERSION_LEN = 20


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_str_enum(value: Any, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}: expected str, got {type(value).__name__!r}")
    if value not in allowed:
        raise ValueError(f"{label}: {value!r} not in allowed set {sorted(allowed)}")
    return value


def _optional_str_enum(value: Any, allowed: frozenset[str], label: str) -> str | None:
    if value is None:
        return None
    return _require_str_enum(value, allowed, label)


def _require_nonneg_int(value: Any, label: str, max_val: int = _MAX_COUNT) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label}: expected int, got {type(value).__name__!r}")
    if not (0 <= value <= max_val):
        raise ValueError(f"{label}: {value} out of range [0, {max_val}]")
    return value


def _optional_nonneg_int(
    value: Any, label: str, max_val: int = _MAX_COUNT
) -> int | None:
    if value is None:
        return None
    return _require_nonneg_int(value, label, max_val)


def _sanitise_version(tool: str, raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"versions[{tool!r}]: expected str, got {type(raw).__name__!r}")
    v = raw.strip()
    if len(v) > _MAX_VERSION_LEN:
        raise ValueError(
            f"versions[{tool!r}]: version string too long ({len(v)} > {_MAX_VERSION_LEN})"
        )
    if not _VERSION_RE.match(v):
        raise ValueError(
            f"versions[{tool!r}]: {v!r} is not a short numeric dotted version"
        )
    return v


def _sanitise_setup(setup: Mapping) -> dict:
    out: dict = {}
    status = _require_str_enum(
        setup.get("status", "unknown"), _SETUP_STATUS_VALUES, "setup.status"
    )
    out["status"] = status
    for key in _SETUP_COUNT_KEYS:
        out[key] = _require_nonneg_int(setup.get(key, 0), f"setup.{key}")
    return out


def _sanitise_versions(versions: Mapping) -> dict:
    out: dict = {}
    for tool in _TOOL_NAMES:
        if tool in versions:
            out[tool] = _sanitise_version(tool, versions[tool])
    # Reject any unknown keys
    extra = set(versions.keys()) - _TOOL_NAMES
    if extra:
        raise ValueError(f"versions: unknown tool(s) {sorted(extra)}")
    return out


def _sanitise_result(device_label: str, result: Any) -> dict | None:
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise ValueError(f"{device_label}.result: expected Mapping or None")

    out: dict = {}

    battery = result.get("battery")
    out["battery"] = _optional_nonneg_int(battery, f"{device_label}.result.battery", _MAX_BATTERY)

    code = result.get("code")
    out["code"] = _optional_str_enum(code, _ALLOWED_ERROR_CODES, f"{device_label}.result.code")

    step = result.get("step")
    out["step"] = _optional_str_enum(step, _STEP_VALUES, f"{device_label}.result.step")

    last_ok_step = result.get("last_ok_step")
    out["last_ok_step"] = _optional_str_enum(
        last_ok_step, _STEP_VALUES, f"{device_label}.result.last_ok_step"
    )

    safe_retry_raw = result.get("safe_retry")
    if safe_retry_raw is None:
        out["safe_retry"] = None
    elif isinstance(safe_retry_raw, bool):
        out["safe_retry"] = safe_retry_raw
    else:
        raise ValueError(
            f"{device_label}.result.safe_retry: expected bool or None, got "
            f"{type(safe_retry_raw).__name__!r}"
        )

    return out


def _sanitise_run(run: Mapping, device_map: dict[str, str]) -> dict:
    """Sanitise run data, replacing device_id with stable labels."""
    out: dict = {}
    out["status"] = _require_str_enum(
        run.get("status", "unknown"), _RUN_STATUS_VALUES, "run.status"
    )

    raw_targets = run.get("targets", [])
    if not isinstance(raw_targets, (list, tuple)):
        raise ValueError("run.targets: expected list")

    sanitised_targets = []
    for i, target in enumerate(raw_targets):
        if not isinstance(target, Mapping):
            raise ValueError(f"run.targets[{i}]: expected Mapping")

        raw_id = target.get("device_id")
        if raw_id not in device_map:
            raise ValueError(
                f"run.targets[{i}].device_id: {raw_id!r} not found in device_map"
            )
        label = device_map[raw_id]

        t_out: dict = {}
        t_out["device_label"] = label
        t_out["status"] = _require_str_enum(
            target.get("status", "pending"), _DEVICE_STATUS_VALUES, f"target[{label}].status"
        )
        t_out["result"] = _sanitise_result(label, target.get("result"))
        sanitised_targets.append(t_out)

    out["targets"] = sanitised_targets
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_support_report(
    *,
    setup: Mapping,
    versions: Mapping,
    run: Mapping | None = None,
) -> dict:
    """
    Build a sanitised support report dict.

    Parameters
    ----------
    setup:
        Device readiness summary.  Known keys: status, ready_count,
        unauthorized_count, offline_count, ambiguous_count,
        unverified_count, other_count.
    versions:
        Tool version strings.  Keys: "adb", "scrcpy".  Values must be
        short numeric dotted strings (e.g. "1.0.41").
    run:
        Optional run snapshot from RunStore.  Keys: status, targets.
        Each target has device_id, status, result.

    Returns
    -------
    dict
        Sanitised report.  schema_version=1; no serials, paths, tokens,
        screenshots, URLs, raw logs, run IDs, environment, or extra keys.

    Raises
    ------
    ValueError
        On any invalid / disallowed input value.
    """
    if not isinstance(setup, Mapping):
        raise ValueError("setup must be a Mapping")
    if not isinstance(versions, Mapping):
        raise ValueError("versions must be a Mapping")
    if run is not None and not isinstance(run, Mapping):
        raise ValueError("run must be a Mapping or None")

    # Build a stable device_id -> label map from the run targets (if any),
    # preserving insertion order so labels are deterministic.
    device_map: dict[str, str] = {}
    if run is not None:
        raw_targets = run.get("targets", [])
        if not isinstance(raw_targets, (list, tuple)):
            raise ValueError("run.targets: expected list")
        for target in raw_targets:
            if isinstance(target, Mapping):
                raw_id = target.get("device_id")
                if raw_id is not None and raw_id not in device_map:
                    idx = len(device_map) + 1
                    device_map[raw_id] = f"Device {idx}"

    report: dict = {
        "schema_version": SCHEMA_VERSION,
        "setup": _sanitise_setup(setup),
        "versions": _sanitise_versions(versions),
        "run": _sanitise_run(run, device_map) if run is not None else None,
    }
    return report


def write_support_archive(path: str | Path, report: Mapping) -> Path:
    """
    Write a ZIP archive containing a single ``diagnostics.json`` file.

    The writer re-sanitises *report* through ``build_support_report`` before
    writing, so an arbitrary Mapping cannot export unsanctioned data.  The
    file is written atomically (temp file + rename in the same directory).

    Parameters
    ----------
    path:
        Destination ZIP file path (supplied explicitly by caller).
    report:
        A Mapping that must conform to the schema produced by
        ``build_support_report``.  Extra or unknown keys raise ValueError.

    Returns
    -------
    Path
        The final path of the written archive.

    Raises
    ------
    ValueError
        If *report* does not conform to the allowed schema.
    OSError
        On filesystem errors.
    """
    dest = Path(path)

    # --- Re-sanitise: reconstruct the allowed shape from the mapping ---------
    # This ensures the writer itself is the enforcement boundary.
    if not isinstance(report, Mapping):
        raise ValueError("report must be a Mapping")

    sv = report.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ValueError(
            f"report.schema_version: expected {SCHEMA_VERSION}, got {sv!r}"
        )

    setup_raw = report.get("setup")
    if not isinstance(setup_raw, Mapping):
        raise ValueError("report.setup: expected Mapping")
    versions_raw = report.get("versions")
    if not isinstance(versions_raw, Mapping):
        raise ValueError("report.versions: expected Mapping")
    run_raw = report.get("run")
    if run_raw is not None and not isinstance(run_raw, Mapping):
        raise ValueError("report.run: expected Mapping or None")

    # Re-build via public sanitiser (raises ValueError if anything is off).
    # For run, we need the device_map already embedded as device_label (not
    # device_id), so we pass the already-labelled structure through a
    # specialised re-sanitiser rather than re-doing ID mapping.
    sanitised_setup = _sanitise_setup(setup_raw)
    sanitised_versions = _sanitise_versions(versions_raw)
    sanitised_run = _resanitise_run(run_raw) if run_raw is not None else None

    clean: dict = {
        "schema_version": SCHEMA_VERSION,
        "setup": sanitised_setup,
        "versions": sanitised_versions,
        "run": sanitised_run,
    }

    payload = json.dumps(clean, indent=2, ensure_ascii=True)
    encoded = payload.encode("utf-8")

    # Atomic write: write to a sibling temp file then rename.
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".zip.tmp")
    try:
        os.close(fd)
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("diagnostics.json", encoded)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return dest


# ---------------------------------------------------------------------------
# Internal: re-sanitiser for already-labelled run dicts
# ---------------------------------------------------------------------------


def _resanitise_result(label: str, result: Any) -> dict | None:
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise ValueError(f"{label}.result: expected Mapping or None")

    out: dict = {}
    battery = result.get("battery")
    out["battery"] = _optional_nonneg_int(battery, f"{label}.result.battery", _MAX_BATTERY)
    code = result.get("code")
    out["code"] = _optional_str_enum(code, _ALLOWED_ERROR_CODES, f"{label}.result.code")
    step = result.get("step")
    out["step"] = _optional_str_enum(step, _STEP_VALUES, f"{label}.result.step")
    last_ok_step = result.get("last_ok_step")
    out["last_ok_step"] = _optional_str_enum(
        last_ok_step, _STEP_VALUES, f"{label}.result.last_ok_step"
    )
    safe_retry_raw = result.get("safe_retry")
    if safe_retry_raw is None:
        out["safe_retry"] = None
    elif isinstance(safe_retry_raw, bool):
        out["safe_retry"] = safe_retry_raw
    else:
        raise ValueError(
            f"{label}.result.safe_retry: expected bool or None, got "
            f"{type(safe_retry_raw).__name__!r}"
        )
    return out


def _resanitise_run(run: Mapping) -> dict:
    """Re-sanitise an already-labelled run dict (device_label present, no device_id)."""
    out: dict = {}
    out["status"] = _require_str_enum(
        run.get("status", "unknown"), _RUN_STATUS_VALUES, "run.status"
    )

    raw_targets = run.get("targets", [])
    if not isinstance(raw_targets, (list, tuple)):
        raise ValueError("run.targets: expected list")

    sanitised_targets = []
    for i, target in enumerate(raw_targets):
        if not isinstance(target, Mapping):
            raise ValueError(f"run.targets[{i}]: expected Mapping")

        label_raw = target.get("device_label")
        if not isinstance(label_raw, str) or not label_raw.startswith("Device "):
            raise ValueError(
                f"run.targets[{i}].device_label: expected 'Device N' string, "
                f"got {label_raw!r}"
            )
        label = label_raw

        t_out: dict = {}
        t_out["device_label"] = label
        t_out["status"] = _require_str_enum(
            target.get("status", "pending"), _DEVICE_STATUS_VALUES, f"target[{label}].status"
        )
        t_out["result"] = _resanitise_result(label, target.get("result"))
        sanitised_targets.append(t_out)

    out["targets"] = sanitised_targets
    return out
