"""PyInstaller macOS runtime hook for DroidHub Desktop.

Configures environment variables and binary search paths for macOS .app bundle:
- Sets DROIDHUB_ADB_PATH and DROIDHUB_SCRCPY_PATH from sys._MEIPASS resource tools directory.
- Respects explicit user overrides.
- Ensures adjacent scrcpy-server sidecar is visible across PyInstaller symlink trees.
- Augments PATH for GUI application launches from Finder with reduced PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_tools_dir(base_dir: Path | None = None) -> Path | None:
    """Locate the tools directory containing adb, scrcpy, and scrcpy-server."""
    candidates: list[Path] = []

    if base_dir:
        candidates.extend([
            base_dir / "tools",
            base_dir / "Resources" / "tools",
            base_dir.parent / "Resources" / "tools",
            base_dir.parent / "MacOS" / "tools",
        ])

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        m_path = Path(meipass)
        candidates.extend([
            m_path / "tools",
            m_path / "Resources" / "tools",
            m_path.parent / "Resources" / "tools",
            m_path.parent / "MacOS" / "tools",
            m_path.parent / "Frameworks" / "tools",
        ])

    exe = getattr(sys, "executable", None)
    if exe:
        exe_path = Path(exe)
        candidates.extend([
            exe_path.parent / "tools",
            exe_path.parent.parent / "Resources" / "tools",
            exe_path.parent.parent / "Frameworks" / "tools",
        ])

    # First pass: find a candidate directory that contains any of the tool files
    for cand in candidates:
        try:
            if cand.is_dir() and any(
                (cand / name).exists()
                for name in ("adb", "scrcpy", "scrcpy-server")
            ):
                return cand.resolve()
        except Exception:
            continue

    # Second pass: fallback to any candidate that exists as a directory
    for cand in candidates:
        try:
            if cand.is_dir():
                return cand.resolve()
        except Exception:
            continue

    return None


def setup_macos_runtime(base_dir: Path | None = None) -> None:
    """Configure runtime environment for native macOS DroidHub application."""
    tools_dir = find_tools_dir(base_dir)

    # 1. Configure PATH for GUI launches from Finder with reduced PATH
    current_path = os.environ.get("PATH", "")
    path_parts = [p for p in current_path.split(os.pathsep) if p]

    if tools_dir and str(tools_dir) not in path_parts:
        path_parts.insert(0, str(tools_dir))

    # Finder / launchd launches provide a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin).
    # Include standard macOS tool locations if available on disk.
    mac_standard_dirs = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for d in mac_standard_dirs:
        if d not in path_parts and os.path.isdir(d):
            path_parts.append(d)

    os.environ["PATH"] = os.pathsep.join(path_parts)

    if not tools_dir:
        return

    # 2. Set DROIDHUB_ADB_PATH if not explicitly overridden by user
    if not os.environ.get("DROIDHUB_ADB_PATH"):
        adb_cand = tools_dir / "adb"
        if adb_cand.is_file():
            try:
                adb_cand.chmod(adb_cand.stat().st_mode | 0o111)
            except Exception:
                pass
            os.environ["DROIDHUB_ADB_PATH"] = str(adb_cand)

    # 3. Set DROIDHUB_SCRCPY_PATH if not explicitly overridden by user
    if not os.environ.get("DROIDHUB_SCRCPY_PATH"):
        scrcpy_cand = tools_dir / "scrcpy"
        if scrcpy_cand.is_file():
            try:
                scrcpy_cand.chmod(scrcpy_cand.stat().st_mode | 0o111)
            except Exception:
                pass
            os.environ["DROIDHUB_SCRCPY_PATH"] = str(scrcpy_cand)

    # 4. Handle scrcpy-server and adjacent visibility across PyInstaller symlink trees
    server_cand = tools_dir / "scrcpy-server"
    if server_cand.is_file():
        if not os.environ.get("SCRCPY_SERVER_PATH"):
            os.environ["SCRCPY_SERVER_PATH"] = str(server_cand)

        # Make sure adjacent server is present next to active scrcpy
        active_scrcpy = os.environ.get("DROIDHUB_SCRCPY_PATH")
        if active_scrcpy:
            active_path = Path(active_scrcpy)
            targets: list[Path] = [active_path.parent]
            try:
                resolved_parent = active_path.resolve().parent
                if resolved_parent not in targets:
                    targets.append(resolved_parent)
            except Exception:
                pass

            for target_dir in targets:
                if target_dir.is_dir():
                    adj_server = target_dir / "scrcpy-server"
                    if not adj_server.exists():
                        try:
                            adj_server.symlink_to(server_cand)
                        except Exception:
                            try:
                                shutil.copy2(server_cand, adj_server)
                            except Exception:
                                pass


if (
    getattr(sys, "frozen", False)
    or getattr(sys, "_MEIPASS", None)
    or os.environ.get("DROIDHUB_FORCE_RUNTIME_HOOK")
):
    setup_macos_runtime()
