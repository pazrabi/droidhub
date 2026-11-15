"""CLI packaging tool for local native Apple Silicon macOS DroidHub.app using PyInstaller.

Accepts an official scrcpy macOS arm64 directory containing adb, scrcpy,
scrcpy-server, and LICENSE, stages artifacts and PyInstaller spec under
an explicit --work-dir, and packages DroidHub.app into --dist-dir.
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
    raise SystemExit("Python >= 3.11 is required to package DroidHub macOS application.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("package_macos")


def validate_scrcpy_dir(scrcpy_dir: Path) -> dict[str, Path]:
    """Validate that the supplied scrcpy directory contains required tools."""
    if not scrcpy_dir.exists():
        raise FileNotFoundError(f"scrcpy directory does not exist: {scrcpy_dir}")
    if not scrcpy_dir.is_dir():
        raise NotADirectoryError(f"scrcpy path is not a directory: {scrcpy_dir}")

    adb_bin = scrcpy_dir / "adb"
    scrcpy_bin = scrcpy_dir / "scrcpy"
    server_bin = scrcpy_dir / "scrcpy-server"

    # Find license file (LICENSE, LICENSE.txt, etc.)
    license_file: Path | None = None
    for cand in [scrcpy_dir / "LICENSE", scrcpy_dir / "LICENSE.txt", scrcpy_dir / "license"]:
        if cand.is_file():
            license_file = cand
            break
    if not license_file:
        for cand in scrcpy_dir.glob("LICENSE*"):
            if cand.is_file():
                license_file = cand
                break

    missing: list[str] = []
    if not adb_bin.is_file():
        missing.append("adb")
    if not scrcpy_bin.is_file():
        missing.append("scrcpy")
    if not server_bin.is_file():
        missing.append("scrcpy-server")
    if not license_file:
        missing.append("LICENSE")

    if missing:
        raise FileNotFoundError(
            f"scrcpy directory '{scrcpy_dir}' is missing required files: {', '.join(missing)}"
        )

    return {
        "adb": adb_bin,
        "scrcpy": scrcpy_bin,
        "scrcpy-server": server_bin,
        "license": license_file,
    }


def stage_tools(scrcpy_dir: Path, work_dir: Path) -> Path:
    """Stage scrcpy and adb binaries and sidecars under work-dir/tools."""
    tools_staging = work_dir / "tools"
    tools_staging.mkdir(parents=True, exist_ok=True)

    # Copy all files from scrcpy directory into staging
    for item in scrcpy_dir.iterdir():
        dest = tools_staging / item.name
        if item.is_file():
            shutil.copy2(item, dest)
        elif item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)

    # Ensure executable permissions on binaries
    for bin_name in ("adb", "scrcpy"):
        b = tools_staging / bin_name
        if b.is_file():
            try:
                b.chmod(b.stat().st_mode | 0o755)
            except Exception:
                pass

    return tools_staging


def generate_spec(
    spec_path: Path,
    repo_root: Path,
    entry_point: Path,
    runtime_hook: Path,
    tools_staging: Path,
    app_name: str = "DroidHub",
) -> None:
    """Generate PyInstaller spec file for macOS onedir/windowed build."""
    staged_items = sorted(tools_staging.iterdir())
    datas_list = [(str(item.resolve()), "tools") for item in staged_items]

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

block_cipher = None

repo_root = {repr(str(repo_root.resolve()))}
entry_point = {repr(str(entry_point.resolve()))}
runtime_hook = {repr(str(runtime_hook.resolve()))}

datas = {repr(datas_list)}

hidden_imports = [
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'av',
]

try:
    from PyInstaller.utils.hooks import collect_submodules
    hidden_imports.extend(collect_submodules('PIL'))
except Exception:
    pass

try:
    from PyInstaller.utils.hooks import collect_submodules
    hidden_imports.extend(collect_submodules('av'))
except Exception:
    pass

a = Analysis(
    [entry_point],
    pathex=[repo_root],
    binaries=[],
    datas=datas,
    hiddenimports=list(dict.fromkeys(hidden_imports)),
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[runtime_hook],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={repr(app_name)},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name={repr(app_name)},
)

app = BUNDLE(
    coll,
    name={repr(f"{app_name}.app")},
    icon=None,
    bundle_identifier='com.droidhub.desktop',
    info_plist={{
        'CFBundleName': {repr(app_name)},
        'CFBundleDisplayName': {repr(app_name)},
        'CFBundleIdentifier': 'com.droidhub.desktop',
        'CFBundleVersion': '0.1.0',
        'CFBundleShortVersionString': '0.1.0',
        'NSHighResolutionCapable': 'True',
        'LSMinimumSystemVersion': '11.0',
    }},
)
"""
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec_content, encoding="utf-8")
    logger.info("Generated PyInstaller spec at %s", spec_path)


def post_process_app_bundle(app_bundle: Path) -> None:
    """Ensure tool permissions and adjacent scrcpy-server visibility in .app bundle."""
    if not app_bundle.is_dir():
        return

    contents = app_bundle / "Contents"
    res_tools = contents / "Resources" / "tools"
    macos_tools = contents / "MacOS" / "tools"
    frameworks_tools = contents / "Frameworks" / "tools"

    # Ensure executable permissions on binaries
    for t_dir in [res_tools, macos_tools, frameworks_tools]:
        if t_dir.is_dir():
            for bin_name in ("adb", "scrcpy"):
                b_path = t_dir / bin_name
                if b_path.exists() and not b_path.is_symlink():
                    try:
                        b_path.chmod(b_path.stat().st_mode | 0o755)
                    except Exception:
                        pass

    # Find the scrcpy-server source
    server_source: Path | None = None
    for t_dir in [res_tools, macos_tools, frameworks_tools]:
        cand = t_dir / "scrcpy-server"
        if cand.is_file():
            server_source = cand
            break

    # Ensure adjacent scrcpy-server exists wherever scrcpy binary is present
    if server_source:
        for t_dir in [res_tools, macos_tools, frameworks_tools]:
            if t_dir.is_dir() and (t_dir / "scrcpy").exists():
                server_dest = t_dir / "scrcpy-server"
                if not server_dest.exists():
                    try:
                        server_dest.symlink_to(server_source)
                    except Exception:
                        try:
                            shutil.copy2(server_source, server_dest)
                        except Exception:
                            pass

    # Ensure MacOS and Frameworks symlink to Resources/tools if absent
    if res_tools.is_dir():
        for parent_dir in [contents / "MacOS", contents / "Frameworks"]:
            if parent_dir.is_dir():
                target_tools = parent_dir / "tools"
                if not target_tools.exists():
                    try:
                        target_tools.symlink_to("../Resources/tools")
                    except Exception:
                        pass


def build_macos_app(
    scrcpy_dir: Path,
    dist_dir: Path,
    work_dir: Path,
    app_name: str = "DroidHub",
) -> int:
    """Stage work and spec, and build macOS app bundle if on Darwin."""
    scrcpy_dir = scrcpy_dir.resolve()
    dist_dir = dist_dir.resolve()
    work_dir = work_dir.resolve()

    logger.info("Validating scrcpy directory: %s", scrcpy_dir)
    validate_scrcpy_dir(scrcpy_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parent.parent
    entry_point = repo_root / "desktop" / "main.py"
    runtime_hook = repo_root / "desktop" / "packaging" / "macos_runtime.py"

    logger.info("Staging tools under %s", work_dir / "tools")
    tools_staging = stage_tools(scrcpy_dir, work_dir)

    spec_path = work_dir / f"{app_name}.spec"
    logger.info("Generating spec file under %s", spec_path)
    generate_spec(
        spec_path=spec_path,
        repo_root=repo_root,
        entry_point=entry_point,
        runtime_hook=runtime_hook,
        tools_staging=tools_staging,
        app_name=app_name,
    )

    # Check if host is macOS Darwin
    if platform.system() != "Darwin" and not os.environ.get("DROIDHUB_FORCE_BUILD"):
        logger.info(
            "Current platform is '%s'. PyInstaller Apple Silicon macOS build must be run on macOS. "
            "Staging and spec generation completed successfully under %s.",
            platform.system(),
            work_dir,
        )
        return 0

    # Ensure PyInstaller is installed
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        logger.error(
            "PyInstaller is not installed in the current Python environment. "
            "Please install PyInstaller (e.g. 'pip install pyinstaller')."
        )
        return 1

    build_dir = work_dir / "build"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        str(spec_path),
    ]

    logger.info("Executing PyInstaller command: %s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        logger.error("PyInstaller failed with return code %d", proc.returncode)
        return proc.returncode

    app_bundle = dist_dir / f"{app_name}.app"
    logger.info("Post-processing app bundle at %s", app_bundle)
    post_process_app_bundle(app_bundle)

    logger.info("macOS native application successfully built at %s", app_bundle)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m desktop.package_macos",
        description="Build native Apple Silicon macOS DroidHub.app with PyInstaller.",
    )
    parser.add_argument(
        "--scrcpy-dir",
        required=True,
        type=Path,
        help="Path to official scrcpy macOS arm64 directory with adb, scrcpy, scrcpy-server, LICENSE",
    )
    parser.add_argument(
        "--dist-dir",
        required=True,
        type=Path,
        help="Output directory where DroidHub.app will be placed",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="Work directory for staging artifacts and PyInstaller spec",
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
        return build_macos_app(
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
