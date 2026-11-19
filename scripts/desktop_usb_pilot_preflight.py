#!/usr/bin/env python3
"""
scripts/desktop_usb_pilot_preflight.py - Standalone stdlib preflight check for DroidHub Desktop USB pilot.

Verifies:
- macOS arm64 platform (Darwin arm64/aarch64).
- App bundle: Contents/MacOS/DroidHub and Contents/Resources/tools/adb or
  Contents/Frameworks/tools/adb (regular executable files; symlinks must resolve inside bundle).
- Desktop PID: ps -p PID -o pid=,comm= matches exact DroidHub binary; error/unreadable -> incomplete.
- Workspace: exists, is a directory, writable; free space reported without invented threshold.
- ADB: bundled adb devices -l with short timeout, no shell. Matches 2 target serials,
  requires state=device, usb: token, positive transport_id. Discards network, emulator, duplicates.
  Crash, failure, timeout, or daemon start/restart -> incomplete.
- Privacy: report contains ONLY counters and statuses, NO serials, NO PII paths, NO raw output.
- No files created, no Desktop lifecycle modifications, no network calls to Hub.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Sequence

# Regex patterns for adb devices parsing and daemon detection
DAEMON_MESSAGE_RE = re.compile(
    r"(\* daemon not running|\* daemon started|starting (?:it )?now|killing\.{3}|server version .* doesn't match)",
    re.IGNORECASE,
)
EMULATOR_SERIAL_RE = re.compile(r"^emulator-\d+$", re.IGNORECASE)
NETWORK_SERIAL_RE = re.compile(r"^.+:\d+$")


@dataclass(frozen=True)
class AdbCheckResult:
    status: str  # "ok", "incomplete", "fail"
    targets_matched: int
    usb_detected: int
    network_filtered: int
    emulator_filtered: int
    duplicate_filtered: int


def check_platform(
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Verifies Darwin arm64 / aarch64 platform."""
    if system is None:
        system = platform.system()
    if machine is None:
        machine = platform.machine()

    is_darwin = system.strip().lower() == "darwin"
    is_arm64 = machine.strip().lower() in ("arm64", "aarch64")
    return "ok" if (is_darwin and is_arm64) else "fail"


def _verify_executable_file(file_path: Path, bundle_root: Path) -> tuple[bool, Path | None]:
    """
    Checks that file_path exists, is a regular file (or symlink to a regular file inside bundle_root),
    and is executable.
    Returns (True, resolved_path) if valid, otherwise (False, None).
    """
    try:
        if not file_path.exists():
            return False, None

        bundle_real = bundle_root.resolve()

        if file_path.is_symlink():
            resolved = file_path.resolve()
            try:
                resolved.relative_to(bundle_real)
            except ValueError:
                # Symlink escapes bundle
                return False, None

            if not resolved.is_file():
                return False, None
            if not os.access(resolved, os.X_OK):
                return False, None
            return True, resolved
        else:
            if not file_path.is_file():
                return False, None
            if not os.access(file_path, os.X_OK):
                return False, None
            return True, file_path.resolve()
    except (OSError, ValueError):
        return False, None


def check_app_bundle(app_path: Path) -> tuple[str, Path | None, Path | None]:
    """
    Verifies DroidHub executable and adb binary.
    Returns (status, droidhub_bin_path, adb_bin_path).
    status is 'ok' or 'fail'.
    """
    if not app_path.exists() or not app_path.is_dir():
        return "fail", None, None

    droidhub_candidate = app_path / "Contents" / "MacOS" / "DroidHub"
    droidhub_ok, droidhub_resolved = _verify_executable_file(droidhub_candidate, app_path)
    if not droidhub_ok or droidhub_resolved is None:
        return "fail", None, None

    adb_candidates = [
        app_path / "Contents" / "Resources" / "tools" / "adb",
        app_path / "Contents" / "Frameworks" / "tools" / "adb",
    ]
    adb_resolved = None
    for candidate in adb_candidates:
        adb_ok, res = _verify_executable_file(candidate, app_path)
        if adb_ok and res is not None:
            adb_resolved = res
            break

    if adb_resolved is None:
        return "fail", droidhub_resolved, None

    return "ok", droidhub_resolved, adb_resolved


def check_desktop_pid(
    desktop_pid: int,
    expected_bin: Path | None,
    timeout: float = 5.0,
) -> str:
    """
    Verifies via ps -p PID -o pid=,comm= that PID belongs to exact DroidHub binary.
    Returns 'ok', 'incomplete', or 'fail'.
    """
    if desktop_pid <= 0 or expected_bin is None:
        return "fail"

    try:
        proc = subprocess.run(
            ["ps", "-p", str(desktop_pid), "-o", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return "incomplete"

    if proc.returncode != 0:
        return "incomplete"

    output = proc.stdout.strip()
    if not output:
        return "incomplete"

    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if not lines:
        return "incomplete"

    parts = lines[0].split(None, 1)
    if len(parts) != 2:
        return "incomplete"

    try:
        reported_pid = int(parts[0])
    except ValueError:
        return "incomplete"

    if reported_pid != desktop_pid:
        return "incomplete"

    comm = parts[1].strip()
    if not comm:
        return "incomplete"

    try:
        comm_real = Path(comm).resolve()
        expected_real = expected_bin.resolve()
        if comm_real == expected_real:
            return "ok"
    except (OSError, ValueError):
        pass

    return "fail"


def check_workspace(workspace_path: Path) -> tuple[str, int]:
    """
    Checks that workspace exists, is a directory, and is writable.
    Returns (status, free_bytes).
    status is 'ok' or 'fail'.
    """
    try:
        if not workspace_path.exists():
            return "fail", 0
        if not workspace_path.is_dir():
            return "fail", 0
        if not os.access(workspace_path, os.W_OK):
            return "fail", 0

        usage = shutil.disk_usage(workspace_path)
        return "ok", usage.free
    except OSError:
        return "fail", 0


def parse_adb_devices_output(stdout: str, target_serials: Sequence[str]) -> AdbCheckResult:
    """
    Parses adb devices -l output strictly:
    - Filters out network devices (ip:port).
    - Filters out emulator devices (emulator-*).
    - Filters out duplicate serials.
    - Requires usb: token and positive transport_id.
    - Filters out duplicate transport_id (ambiguous transport).
    - Requires state == 'device' for matched targets.
    """
    raw_lines: list[tuple[str, str, list[str]]] = []
    all_serials: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        state = parts[1]
        extra_tokens = parts[2:]
        all_serials.append(serial)
        raw_lines.append((serial, state, extra_tokens))

    serial_counts = Counter(all_serials)

    network_filtered = 0
    emulator_filtered = 0
    duplicate_filtered = 0

    candidate_devices: list[dict[str, Any]] = []

    for serial, state, tokens in raw_lines:
        if NETWORK_SERIAL_RE.match(serial) or serial.startswith("tcp:") or serial.startswith("udp:"):
            network_filtered += 1
            continue

        if EMULATOR_SERIAL_RE.match(serial):
            emulator_filtered += 1
            continue

        if serial_counts[serial] > 1:
            duplicate_filtered += 1
            continue

        has_usb = any(t.startswith("usb:") and len(t) > 4 for t in tokens)
        tid = None
        for t in tokens:
            if t.startswith("transport_id:"):
                try:
                    val = int(t[13:])
                    if val > 0:
                        tid = val
                except ValueError:
                    pass
                break

        if not has_usb or tid is None:
            # Not a valid USB physical device with positive transport_id
            continue

        candidate_devices.append({
            "serial": serial,
            "state": state,
            "tid": tid,
        })

    # Ambiguous transport check: duplicate transport_id among candidate devices
    tid_counts = Counter(d["tid"] for d in candidate_devices)
    valid_devices: dict[str, dict[str, Any]] = {}
    for d in candidate_devices:
        if tid_counts[d["tid"]] > 1:
            duplicate_filtered += 1
            continue
        valid_devices[d["serial"]] = d

    usb_detected = sum(1 for d in valid_devices.values() if d["state"] == "device")

    targets_matched = 0
    for target in target_serials:
        if target in valid_devices and valid_devices[target]["state"] == "device":
            targets_matched += 1

    # Both targets must be matched
    status = "ok" if (targets_matched == len(target_serials) and len(target_serials) == 2) else "fail"

    return AdbCheckResult(
        status=status,
        targets_matched=targets_matched,
        usb_detected=usb_detected,
        network_filtered=network_filtered,
        emulator_filtered=emulator_filtered,
        duplicate_filtered=duplicate_filtered,
    )


def check_adb_devices(
    adb_path: Path | None,
    target_serials: Sequence[str],
    timeout: float = 5.0,
) -> AdbCheckResult:
    """
    Runs bundled adb devices -l without shell and with short timeout.
    Detects daemon restart / failure / timeout as incomplete.
    """
    if adb_path is None:
        return AdbCheckResult(
            status="fail",
            targets_matched=0,
            usb_detected=0,
            network_filtered=0,
            emulator_filtered=0,
            duplicate_filtered=0,
        )

    try:
        proc = subprocess.run(
            [str(adb_path), "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AdbCheckResult(
            status="incomplete",
            targets_matched=0,
            usb_detected=0,
            network_filtered=0,
            emulator_filtered=0,
            duplicate_filtered=0,
        )
    except (subprocess.SubprocessError, OSError):
        return AdbCheckResult(
            status="incomplete",
            targets_matched=0,
            usb_detected=0,
            network_filtered=0,
            emulator_filtered=0,
            duplicate_filtered=0,
        )

    if proc.returncode != 0:
        return AdbCheckResult(
            status="incomplete",
            targets_matched=0,
            usb_detected=0,
            network_filtered=0,
            emulator_filtered=0,
            duplicate_filtered=0,
        )

    combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if DAEMON_MESSAGE_RE.search(combined_output):
        return AdbCheckResult(
            status="incomplete",
            targets_matched=0,
            usb_detected=0,
            network_filtered=0,
            emulator_filtered=0,
            duplicate_filtered=0,
        )

    return parse_adb_devices_output(proc.stdout or "", target_serials)


def run_preflight(
    app: str | Path,
    workspace: str | Path,
    desktop_pid: int,
    devices: Sequence[str],
    *,
    platform_system: str | None = None,
    platform_machine: str | None = None,
    adb_timeout: float = 5.0,
    ps_timeout: float = 5.0,
) -> dict[str, Any]:
    """
    Runs full standalone preflight check.
    Returns sanitised report containing ONLY statuses and counters.
    No serials, PII paths, or raw output.
    """
    app_path = Path(app)
    workspace_path = Path(workspace)

    # 1. Platform check
    platform_status = check_platform(system=platform_system, machine=platform_machine)

    # 2. App bundle check
    app_status, droidhub_bin, adb_bin = check_app_bundle(app_path)

    # 3. Desktop PID check
    pid_status = check_desktop_pid(desktop_pid, droidhub_bin, timeout=ps_timeout)

    # 4. Workspace check
    workspace_status, free_bytes = check_workspace(workspace_path)

    # 5. Bundled ADB devices check
    adb_res = check_adb_devices(adb_bin, devices, timeout=adb_timeout)

    checks = {
        "platform": platform_status,
        "app": app_status,
        "desktop_pid": pid_status,
        "workspace": workspace_status,
        "devices": adb_res.status,
    }

    # Incomplete if any check could not be completed cleanly; fail if any check definitively failed; else ok
    if any(s == "incomplete" for s in checks.values()):
        overall_status = "incomplete"
    elif any(s == "fail" for s in checks.values()):
        overall_status = "fail"
    else:
        overall_status = "ok"

    return {
        "status": overall_status,
        "overall_status": overall_status,
        "checks": checks,
        "counters": {
            "targets_expected": len(devices),
            "targets_matched": adb_res.targets_matched,
            "usb_devices_detected": adb_res.usb_detected,
            "network_devices_filtered": adb_res.network_filtered,
            "emulator_devices_filtered": adb_res.emulator_filtered,
            "duplicate_devices_filtered": adb_res.duplicate_filtered,
            "workspace_free_bytes": free_bytes,
        },
        "workspace_free_bytes": free_bytes,
        "targets_expected": len(devices),
        "targets_matched": adb_res.targets_matched,
    }


def format_text_report(report: dict[str, Any]) -> str:
    """Formats report into human-readable text without PII, serials, or raw output."""
    lines = [
        "DroidHub Desktop USB Pilot Preflight Report",
        f"Overall status: {report['status']}",
        "",
        "Checks:",
    ]
    for check_name, check_status in report["checks"].items():
        lines.append(f"  {check_name}: {check_status}")
    lines.append("")
    lines.append("Counters:")
    for counter_name, counter_val in report["counters"].items():
        lines.append(f"  {counter_name}: {counter_val}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Autonomous preflight check for DroidHub Desktop USB pilot stand.\n"
            "Notice: running 'adb devices' may start or restart a local ADB server daemon if none is running."
        ),
        epilog=(
            "Notice: 'adb devices' may start or restart a local ADB server daemon if none is running."
        ),
    )
    parser.add_argument(
        "--app",
        required=True,
        help="Path to DroidHub.app bundle",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Path to workspace directory",
    )
    parser.add_argument(
        "--desktop-pid",
        type=int,
        required=True,
        help="PID of running DroidHub process (must be positive integer)",
    )
    parser.add_argument(
        "--device",
        action="append",
        required=True,
        help=(
            "Target device serial (must be specified twice for two distinct targets). "
            "Notice: 'adb devices' may start a local ADB server daemon."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report in JSON format",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.desktop_pid <= 0:
        parser.error("--desktop-pid must be a positive integer (> 0)")

    if not args.device or len(args.device) != 2:
        parser.error("--device must be specified exactly twice for two distinct targets")

    if args.device[0] == args.device[1]:
        parser.error("--device targets must be distinct (got duplicate serials)")

    report = run_preflight(
        app=args.app,
        workspace=args.workspace,
        desktop_pid=args.desktop_pid,
        devices=args.device,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_text_report(report))

    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
