"""
Build script for the native macOS hub-helper binary and its XPC client dylib.

Usage (primary path):
    python -m desktop.packaging.build_macos_hub_helper \\
        --output-dir /path/to/fresh/dir \\
        --signing-identity AABBCC...  # 40-hex SHA-1 cert fingerprint

This script compiles arm64/macOS-12+ native artifacts from sibling source files
in desktop/packaging/ using xcrun-wrapped clang/swiftc.  It never installs
anything, never touches launchd, never modifies an application bundle, and never
accesses the keychain at build time.

Outputs (placed atomically as a matched pair):
    droidhub-hub-helper          — signed Mach-O executable
    macos_hub_xpc_client.dylib   — shared dylib, install_name @rpath/…

stdlib only; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_PYTHON = (3, 9)
_ARCH = "arm64-apple-macosx12.0"
_HELPER_ID = "site.molokohub.droidhub.hub-helper.v1"
_DYLIB_ID = "site.molokohub.droidhub.hub-xpc-client.v1"
_HELPER_OUT = "droidhub-hub-helper"
_DYLIB_OUT = "macos_hub_xpc_client.dylib"
_DYLIB_INSTALL_NAME = f"@rpath/{_DYLIB_OUT}"

# C sources expected as siblings of this script inside desktop/packaging/
_C_MAIN = "macos_hub_helper_main.c"
_C_LISTENER = "macos_hub_helper_listener.c"
_C_KEYCHAIN = "macos_hub_helper_keychain.c"
_C_XPC_CLIENT = "macos_hub_xpc_client.c"

# Swift sources expected as siblings
_SWIFT_INVENTORY = "macos_hub_helper_inventory.swift"
_SWIFT_FETCH = "macos_hub_inventory_fetch.swift"

_FRAMEWORKS = ["Security", "CoreFoundation"]

_SHA1_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _run(args: list[str], *, cwd: Path | None = None) -> None:
    """Run a command, raise on failure with a readable message."""
    try:
        subprocess.run(args, check=True, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        _die(
            f"command failed (exit {exc.returncode}):\n  "
            + " ".join(str(a) for a in args)
        )
    except FileNotFoundError:
        _die(f"executable not found: {args[0]!r}")


def _xcrun(tool: str, sdk_path: str | None) -> list[str]:
    """Return the prefix args to invoke a tool via xcrun (or bare tool)."""
    if sdk_path:
        return ["xcrun", "--sdk", sdk_path, tool]
    return ["xcrun", tool]


def _framework_flags() -> list[str]:
    flags: list[str] = []
    for fw in _FRAMEWORKS:
        flags += ["-framework", fw]
    return flags


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m desktop.packaging.build_macos_hub_helper",
        description="Build the native arm64 macOS hub-helper and XPC client dylib.",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        metavar="DIR",
        help="Fresh (non-existent) directory that will receive the two output files.",
    )
    p.add_argument(
        "--signing-identity",
        required=True,
        metavar="SHA1",
        help="40-hex SHA-1 fingerprint of the signing certificate.",
    )
    p.add_argument(
        "--signing-keychain",
        default=None,
        metavar="KEYCHAIN",
        help="Optional path to a .keychain-db to search for the signing identity.",
    )
    p.add_argument(
        "--sdk-path",
        default=None,
        metavar="SDK",
        help="Optional SDK path passed to xcrun --sdk (e.g. macosx).",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(ns: argparse.Namespace, src_dir: Path) -> None:
    # -- signing identity
    if not _SHA1_RE.match(ns.signing_identity):
        _die(
            "--signing-identity must be exactly 40 hexadecimal characters "
            f"(got {ns.signing_identity!r})"
        )

    # -- output dir must not exist
    out = Path(ns.output_dir)
    if out.exists():
        _die(
            f"--output-dir already exists; refusing to overwrite: {out}\n"
            "  Remove it first or choose a different path."
        )

    # -- signing keychain path (if given) must exist on disk
    if ns.signing_keychain is not None:
        kc = Path(ns.signing_keychain)
        if not kc.exists():
            _die(f"--signing-keychain path does not exist: {kc}")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _codesign_cmd(
    path: Path,
    identifier: str,
    signing_identity: str,
    signing_keychain: str | None,
) -> list[str]:
    cmd = [
        "codesign",
        "--sign", signing_identity,
        "--identifier", identifier,
        "--options", "runtime",
        "--strict",
        "--timestamp",
        "--force",
    ]
    if signing_keychain:
        cmd += ["--keychain", signing_keychain]
    cmd.append(str(path))
    return cmd


def _verify_cmd(path: Path) -> list[str]:
    return ["codesign", "--verify", "--strict", "--deep", str(path)]


def build(ns: argparse.Namespace, src_dir: Path) -> None:
    out_dir = Path(ns.output_dir)
    signing_id = ns.signing_identity.upper()
    sdk_path = ns.sdk_path

    # Build inside a temporary sibling directory; rename atomically on success.
    tmp_dir = out_dir.parent / (out_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        _build_in(
            tmp_dir=tmp_dir,
            src_dir=src_dir,
            signing_id=signing_id,
            signing_keychain=ns.signing_keychain,
            sdk_path=sdk_path,
        )
    except SystemExit:
        # Clean up temp dir on failure before propagating.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # Atomic rename: publish only the complete, signed pair.
    tmp_dir.rename(out_dir)
    print(f"Output written to: {out_dir}")
    print(f"  {out_dir / _HELPER_OUT}")
    print(f"  {out_dir / _DYLIB_OUT}")


def _build_in(
    *,
    tmp_dir: Path,
    src_dir: Path,
    signing_id: str,
    signing_keychain: str | None,
    sdk_path: str | None,
) -> None:
    # Intermediate object files live under a cache subdir.
    cache_dir = tmp_dir / "cache"
    cache_dir.mkdir()

    # -----------------------------------------------------------------------
    # Resolve source paths (may be absent on this branch; script is for the
    # integrated worktree).  We build only what exists.
    # -----------------------------------------------------------------------
    def src(name: str) -> Path:
        return src_dir / name

    clang_prefix = _xcrun("clang", sdk_path)
    swiftc_prefix = _xcrun("swiftc", sdk_path)

    target_flag = ["-target", _ARCH]
    c_werror = ["-Werror"]
    swift_werror = ["-warnings-as-errors"]

    sdk_flags: list[str] = []
    if sdk_path:
        sdk_flags = ["-sdk", sdk_path]

    # -----------------------------------------------------------------------
    # Compile C object files (only if the source is present)
    # -----------------------------------------------------------------------

    c_objects: list[Path] = []

    def compile_c(c_src: Path, extra_defines: list[str] | None = None) -> Path:
        obj = cache_dir / (c_src.stem + ".o")
        cmd = (
            clang_prefix
            + target_flag
            + c_werror
            + ["-c", str(c_src), "-o", str(obj)]
            + (extra_defines or [])
        )
        print(f"[clang] {c_src.name}")
        _run(cmd)
        return obj

    # macos_hub_helper_main.c — inject DH_HUB_SIGNING_CERT_SHA1 as a quoted
    # C string containing the caller-supplied signing identity.
    if src(_C_MAIN).exists():
        cert_define = f'-DDH_HUB_SIGNING_CERT_SHA1="{signing_id}"'
        c_objects.append(compile_c(src(_C_MAIN), [cert_define]))

    if src(_C_LISTENER).exists():
        c_objects.append(compile_c(src(_C_LISTENER)))

    if src(_C_KEYCHAIN).exists():
        c_objects.append(compile_c(src(_C_KEYCHAIN)))

    # -----------------------------------------------------------------------
    # Compile Swift sources into object files
    # -----------------------------------------------------------------------

    swift_objects: list[Path] = []

    swift_srcs = [
        s for s in [src(_SWIFT_INVENTORY), src(_SWIFT_FETCH)] if s.exists()
    ]

    if swift_srcs:
        # Compile all Swift sources together so they share the same module.
        swift_obj = cache_dir / "swift_combined.o"
        cmd = (
            swiftc_prefix
            + sdk_flags
            + target_flag
            + swift_werror
            + ["-c", "-parse-as-library", "-module-name", "HubHelper"]
            + [str(s) for s in swift_srcs]
            + ["-o", str(swift_obj)]
        )
        print(f"[swiftc] {' '.join(s.name for s in swift_srcs)}")
        _run(cmd)
        swift_objects.append(swift_obj)

    # -----------------------------------------------------------------------
    # Link the helper executable
    # -----------------------------------------------------------------------

    helper_path = tmp_dir / _HELPER_OUT

    link_objects = c_objects + swift_objects
    if link_objects:
        cmd = (
            clang_prefix
            + target_flag
            + [str(o) for o in link_objects]
            + _framework_flags()
            + ["-o", str(helper_path)]
        )
        print(f"[link] {_HELPER_OUT}")
        _run(cmd)
    else:
        # No objects to link; create an empty placeholder so the script can
        # still demonstrate the full flow on a branch without sources.
        # (In production the integrated worktree will have all sources.)
        _die("No source files found to build the helper; check the worktree.")

    # -----------------------------------------------------------------------
    # Build the XPC client shared dylib
    # -----------------------------------------------------------------------

    dylib_path = tmp_dir / _DYLIB_OUT

    if src(_C_XPC_CLIENT).exists():
        dylib_obj = cache_dir / "macos_hub_xpc_client.o"
        cmd = (
            clang_prefix
            + target_flag
            + c_werror
            + ["-c", str(src(_C_XPC_CLIENT)), "-o", str(dylib_obj)]
        )
        print(f"[clang] {_C_XPC_CLIENT}")
        _run(cmd)

        cmd = (
            clang_prefix
            + target_flag
            + ["-dynamiclib"]
            + ["-install_name", _DYLIB_INSTALL_NAME]
            + _framework_flags()
            + [str(dylib_obj)]
            + ["-o", str(dylib_path)]
        )
        print(f"[link dylib] {_DYLIB_OUT}")
        _run(cmd)
    else:
        _die(
            f"Source file not found: {src(_C_XPC_CLIENT)}\n"
            "  Build requires the integrated worktree with all source files."
        )

    # -----------------------------------------------------------------------
    # Sign both outputs
    # -----------------------------------------------------------------------

    print(f"[codesign] {_HELPER_OUT}")
    _run(
        _codesign_cmd(
            helper_path,
            identifier=_HELPER_ID,
            signing_identity=signing_id,
            signing_keychain=signing_keychain,
        )
    )

    print(f"[codesign] {_DYLIB_OUT}")
    _run(
        _codesign_cmd(
            dylib_path,
            identifier=_DYLIB_ID,
            signing_identity=signing_id,
            signing_keychain=signing_keychain,
        )
    )

    # -----------------------------------------------------------------------
    # Verify — strict, both artifacts
    # -----------------------------------------------------------------------

    print(f"[verify] {_HELPER_OUT}")
    _run(_verify_cmd(helper_path))

    print(f"[verify] {_DYLIB_OUT}")
    _run(_verify_cmd(dylib_path))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    if sys.version_info < _MIN_PYTHON:
        _die(
            f"Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+ required "
            f"(running {sys.version})"
        )

    ns = _parse_args(argv if argv is not None else sys.argv[1:])

    # src_dir is the directory that contains this script (desktop/packaging/).
    src_dir = Path(__file__).parent.resolve()

    _validate(ns, src_dir)
    build(ns, src_dir)


if __name__ == "__main__":
    main()
