"""Build a native DroidHub.app with the official local scrcpy tools."""

from __future__ import annotations

import argparse
import importlib.util
import platform
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scrcpy-dir", required=True, type=Path)
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("Use a native arm64 Python on macOS to build DroidHub.app.")

    for module in ("PyInstaller", "PIL", "av", "certifi", "_tkinter"):
        if importlib.util.find_spec(module) is None:
            parser.error(f"Missing build dependency: {module}")

    scrcpy_dir = args.scrcpy_dir.resolve()
    work_dir = args.work_dir.resolve()
    dist_dir = args.dist_dir.resolve()
    for name in ("adb", "scrcpy", "scrcpy-server", "LICENSE"):
        if not (scrcpy_dir / name).is_file():
            parser.error(f"Missing {scrcpy_dir / name}")

    desktop_dir = Path(__file__).resolve().parent
    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", "DroidHub",
        "--onedir", "--windowed", "--noconfirm", "--noupx",
        "--target-architecture", "arm64",
        "--osx-bundle-identifier", "site.molokohub.droidhub",
        "--paths", str(desktop_dir.parent),
        "--runtime-hook", str(desktop_dir / "packaging" / "macos_runtime.py"),
        "--hidden-import", "av",
        "--hidden-import", "certifi",
        "--add-binary", f"{scrcpy_dir / 'adb'}:tools",
        "--add-binary", f"{scrcpy_dir / 'scrcpy'}:tools",
        "--add-data", f"{scrcpy_dir / 'scrcpy-server'}:tools",
        "--add-data", f"{scrcpy_dir / 'LICENSE'}:tools",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir / "build"),
        "--specpath", str(work_dir),
        str(desktop_dir / "main.py"),
    ]
    work_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
