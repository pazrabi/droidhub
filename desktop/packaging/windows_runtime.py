"""PyInstaller Windows runtime hook for DroidHub Desktop.

Configures environment variables for Windows native onedir application:
- Sets default DROIDHUB_ADB_PATH, DROIDHUB_SCRCPY_PATH, and SCRCPY_SERVER_PATH
  from the adjacent tools directory bundled with PyInstaller (_MEIPASS / executable).
- Preserves user environment overrides via setdefault.
- Sets verified TLS default certificate bundle via certifi.
- Avoids dummy PATH mutation or modifying target installs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_tools_dir(base_dir: Path | None = None) -> Path | None:
    """Locate the tools directory containing adb.exe and scrcpy.exe."""
    candidates: list[Path] = []

    if base_dir:
        candidates.append(base_dir / "tools")

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        m_path = Path(meipass)
        candidates.extend([
            m_path / "tools",
            m_path.parent / "tools",
        ])

    exe = getattr(sys, "executable", None)
    if exe:
        exe_path = Path(exe)
        candidates.extend([
            exe_path.parent / "tools",
            exe_path.parent / "_internal" / "tools",
        ])

    # First pass: find candidate directory containing adb.exe or scrcpy.exe
    for cand in candidates:
        try:
            if cand.is_dir() and (
                (cand / "adb.exe").is_file() or (cand / "scrcpy.exe").is_file()
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


def setup_windows_runtime(base_dir: Path | None = None) -> None:
    """Configure runtime environment for native Windows DroidHub application."""
    tools_dir = find_tools_dir(base_dir)

    if tools_dir:
        adb_cand = tools_dir / "adb.exe"
        if adb_cand.is_file():
            os.environ.setdefault("DROIDHUB_ADB_PATH", str(adb_cand))

        scrcpy_cand = tools_dir / "scrcpy.exe"
        if scrcpy_cand.is_file():
            os.environ.setdefault("DROIDHUB_SCRCPY_PATH", str(scrcpy_cand))

        server_cand = tools_dir / "scrcpy-server"
        if server_cand.is_file():
            os.environ.setdefault("SCRCPY_SERVER_PATH", str(server_cand))

    # Configure verified TLS default certificate bundle
    try:
        import certifi

        ca_bundle = certifi.where()
        if ca_bundle and os.path.isfile(ca_bundle):
            os.environ.setdefault("SSL_CERT_FILE", ca_bundle)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_bundle)
    except Exception:
        pass


if (
    getattr(sys, "frozen", False)
    or getattr(sys, "_MEIPASS", None)
    or os.environ.get("DROIDHUB_FORCE_RUNTIME_HOOK")
):
    setup_windows_runtime()
