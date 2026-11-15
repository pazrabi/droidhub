"""Build a native Windows x64 DroidHub.exe with the official local scrcpy tools."""

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

    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("Use native x64 Python on Windows to build DroidHub.exe.")
    if sys.maxsize <= 2**32:
        parser.error("Use 64-bit Python to build DroidHub.exe.")

    for module in ("PyInstaller", "PIL", "av", "certifi", "_tkinter"):
        if importlib.util.find_spec(module) is None:
            parser.error(f"Missing build dependency: {module}")

    scrcpy_dir = args.scrcpy_dir.resolve()
    work_dir = args.work_dir.resolve()
    dist_dir = args.dist_dir.resolve()
    required = (
        "adb.exe", "scrcpy.exe", "scrcpy-server", "LICENSE.txt",
        "AdbWinApi.dll", "AdbWinUsbApi.dll", "SDL3.dll",
    )
    tools = [scrcpy_dir / name for name in required]
    for path in tools:
        if not path.is_file():
            parser.error(f"Missing {path}")

    companions = sorted(
        path for path in scrcpy_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".dll", ".png"}
    )
    desktop_dir = Path(__file__).resolve().parent
    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", "DroidHub",
        "--onedir", "--windowed", "--noconfirm", "--noupx",
        "--paths", str(desktop_dir.parent),
        "--runtime-hook", str(desktop_dir / "packaging" / "windows_runtime.py"),
        "--hidden-import", "av",
        "--hidden-import", "certifi",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir / "build"),
        "--specpath", str(work_dir),
    ]
    # The official adb.exe is 32-bit even in the x64 scrcpy archive. Copy
    # the whole external toolchain unchanged, with its DLLs beside the EXEs.
    for path in dict.fromkeys([*tools, *companions]):
        command.extend(("--add-data", f"{path}:tools"))
    command.append(str(desktop_dir / "main.py"))
    work_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
