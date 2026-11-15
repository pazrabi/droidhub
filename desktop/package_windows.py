"""CLI packaging tool for local native Windows x64 DroidHub Desktop using PyInstaller.

Accepts an extracted official scrcpy-win64 directory containing adb.exe,
scrcpy.exe, scrcpy-server, companion DLLs, and licenses.
Stages tools and packages a native Windows x64 onedir/windowed application
into --dist-dir using PyInstaller with an adjacent tools directory.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# Require Python >= 3.11
if sys.version_info < (3, 11):
    raise SystemExit("Python >= 3.11 is required to package DroidHub Windows application.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("package_windows")


def is_windows_x64() -> bool:
    """Check if the current host platform is 64-bit Windows."""
    return (
        platform.system() == "Windows"
        and platform.machine().lower() in ("amd64", "x86_64", "x64")
        and sys.maxsize > 2**32
    )


def validate_scrcpy_dir(scrcpy_dir: Path) -> dict[str, Path | list[Path]]:
    """Validate that the supplied scrcpy directory contains required tools."""
    if not scrcpy_dir.exists():
        raise FileNotFoundError(f"scrcpy directory does not exist: {scrcpy_dir}")
    if not scrcpy_dir.is_dir():
        raise NotADirectoryError(f"scrcpy path is not a directory: {scrcpy_dir}")

    adb_bin = scrcpy_dir / "adb.exe"
    scrcpy_bin = scrcpy_dir / "scrcpy.exe"
    server_bin = scrcpy_dir / "scrcpy-server"

    missing: list[str] = []
    if not adb_bin.is_file():
        missing.append("adb.exe")
    if not scrcpy_bin.is_file():
        missing.append("scrcpy.exe")
    if not server_bin.is_file():
        missing.append("scrcpy-server")

    if missing:
        raise FileNotFoundError(
            f"scrcpy directory '{scrcpy_dir}' is missing required files: {', '.join(missing)}"
        )

    dlls = sorted(scrcpy_dir.glob("*.dll"))
    licenses = [
        p for p in scrcpy_dir.iterdir()
        if p.is_file() and ("license" in p.name.lower() or "notice" in p.name.lower())
    ]

    return {
        "adb": adb_bin,
        "scrcpy": scrcpy_bin,
        "scrcpy-server": server_bin,
        "dlls": dlls,
        "licenses": licenses,
    }


def stage_tools(scrcpy_dir: Path, work_dir: Path) -> Path:
    """Stage scrcpy, adb, companion DLLs, and sidecars under work_dir/tools."""
    tools_staging = work_dir / "tools"
    tools_staging.mkdir(parents=True, exist_ok=True)

    for item in scrcpy_dir.iterdir():
        dest = tools_staging / item.name
        if item.is_file():
            shutil.copy2(item, dest)
        elif item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)

    return tools_staging


def classify_tools(tools_dir: Path) -> tuple[list[Path], list[Path]]:
    """Classify staged tools into PyInstaller binaries and data files.

    Binaries (.exe and .dll) are retained as PyInstaller binaries in the adjacent
    tools directory to avoid import collision and dummy PATH modifications.
    Non-binaries (scrcpy-server, licenses, configs) are classified as data files.
    """
    binaries: list[Path] = []
    datas: list[Path] = []

    for item in sorted(tools_dir.iterdir()):
        if item.is_file():
            if item.suffix.lower() in (".exe", ".dll"):
                binaries.append(item)
            else:
                datas.append(item)
        elif item.is_dir():
            datas.append(item)

    return binaries, datas


def build_windows_app(
    scrcpy_dir: Path,
    dist_dir: Path,
    work_dir: Path,
    app_name: str = "DroidHub",
) -> int:
    """Build Windows x64 native onedir/windowed application with PyInstaller."""
    scrcpy_dir = scrcpy_dir.resolve()
    dist_dir = dist_dir.resolve()
    work_dir = work_dir.resolve()

    logger.info("Validating scrcpy directory: %s", scrcpy_dir)
    validate_scrcpy_dir(scrcpy_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parent.parent
    entry_point = repo_root / "desktop" / "main.py"
    runtime_hook = repo_root / "desktop" / "packaging" / "windows_runtime.py"

    if not entry_point.is_file():
        raise FileNotFoundError(f"Entry point not found: {entry_point}")
    if not runtime_hook.is_file():
        raise FileNotFoundError(f"Runtime hook not found: {runtime_hook}")

    logger.info("Staging tools under %s", work_dir / "tools")
    tools_staging = stage_tools(scrcpy_dir, work_dir)
    binaries, datas = classify_tools(tools_staging)

    # Strict Windows x64 host check
    if not is_windows_x64() and not os.environ.get("DROIDHUB_FORCE_BUILD"):
        logger.error(
            "Packaging requires a Windows x64 host (detected: %s %s).",
            platform.system(),
            platform.machine(),
        )
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        logger.error(
            "PyInstaller is not installed in the current Python environment. "
            "Please install PyInstaller (e.g. 'pip install pyinstaller')."
        )
        return 1

    build_dir = work_dir / "build"
    sep = ";" if platform.system() == "Windows" else os.pathsep

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name",
        app_name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(work_dir),
        "--paths",
        str(repo_root),
        "--runtime-hook",
        str(runtime_hook),
        "--collect-all",
        "certifi",
        "--collect-submodules",
        "PIL",
        "--collect-submodules",
        "av",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "PIL.Image",
        "--hidden-import",
        "PIL.ImageTk",
        "--hidden-import",
        "av",
    ]

    for b in binaries:
        cmd.extend(["--add-binary", f"{b.resolve()}{sep}tools"])
    for d in datas:
        cmd.extend(["--add-data", f"{d.resolve()}{sep}tools"])

    cmd.append(str(entry_point))

    logger.info("Executing PyInstaller command: %s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        logger.error("PyInstaller failed with return code %d", proc.returncode)
        return proc.returncode

    # Ensure adjacent tools directory next to executable in dist folder
    app_dir = dist_dir / app_name
    adjacent_tools = app_dir / "tools"
    if not adjacent_tools.exists() and tools_staging.is_dir():
        shutil.copytree(tools_staging, adjacent_tools)
    elif adjacent_tools.is_dir() and tools_staging.is_dir():
        for item in tools_staging.iterdir():
            dest = adjacent_tools / item.name
            if not dest.exists():
                if item.is_file():
                    shutil.copy2(item, dest)
                elif item.is_dir():
                    shutil.copytree(item, dest)

    logger.info("Windows native application successfully built at %s", app_dir)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m desktop.package_windows",
        description="Build native Windows x64 DroidHub Desktop application with PyInstaller.",
    )
    parser.add_argument(
        "--scrcpy-dir",
        required=True,
        type=Path,
        help="Path to extracted official scrcpy-win64 directory with adb.exe, scrcpy.exe, scrcpy-server",
    )
    parser.add_argument(
        "--dist-dir",
        required=True,
        type=Path,
        help="Output directory where packaged application will be placed",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="Work directory for staging artifacts and PyInstaller files",
    )
    parser.add_argument(
        "--app-name",
        default="DroidHub",
        help="Application name (default: DroidHub)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for CLI packaging."""
    args = parse_args(argv)
    try:
        return build_windows_app(
            scrcpy_dir=args.scrcpy_dir,
            dist_dir=args.dist_dir,
            work_dir=args.work_dir,
            app_name=args.app_name,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        logger.error("Validation error: %s", exc)
        return 1
    except Exception as exc:
        logger.exception("Packaging failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
