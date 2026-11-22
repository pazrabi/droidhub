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

Outputs (placed atomically as a matched pair via renamex_np RENAME_EXCL):
    droidhub-hub-helper          — signed Mach-O executable
    macos_hub_xpc_client.dylib   — shared dylib, install_name @rpath/…

stdlib only; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_PYTHON = (3, 9)
_ARCH = "arm64-apple-macosx12.0"
_MACOS_TARGET = "arm64-apple-macosx12.0"
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

# All six sources that must exist as regular files before building starts.
_REQUIRED_SOURCES = (
    _C_MAIN,
    _C_LISTENER,
    _C_KEYCHAIN,
    _C_XPC_CLIENT,
    _SWIFT_INVENTORY,
    _SWIFT_FETCH,
)

_FRAMEWORKS = ["Security", "CoreFoundation"]

_SHA1_RE = re.compile(r"^[0-9A-Fa-f]{40}$")

# Absolute tool paths — do not rely on PATH for security-sensitive build tools.
_XCRUN = "/usr/bin/xcrun"
_CODESIGN = "/usr/bin/codesign"

# macOS renamex_np flag: RENAME_EXCL — fail if destination already exists.
_RENAME_EXCL = 0x00000004


# ---------------------------------------------------------------------------
# Atomic publish via renamex_np(2)
# ---------------------------------------------------------------------------


def _renamex_np(src: Path, dst: Path) -> None:
    """Atomically rename src → dst with RENAME_EXCL.

    Fails if *dst* already exists (including broken symlinks).  Uses
    renamex_np(2) via ctypes so there is no TOCTOU race.
    """
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        raise OSError("Cannot locate libc for renamex_np")
    libc = ctypes.CDLL(libc_name, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int

    rc = renamex_np(
        str(src).encode(),
        str(dst).encode(),
        ctypes.c_uint(_RENAME_EXCL),
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dst))


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


def _sdk_path() -> str:
    """Return the macOS SDK filesystem path via xcrun --show-sdk-path."""
    try:
        result = subprocess.run(
            [_XCRUN, "--sdk", "macosx", "--show-sdk-path"],
            check=True,
            capture_output=True,
            text=True,
        )
        path = result.stdout.strip()
        if not path:
            _die("xcrun --show-sdk-path returned an empty string")
        return path
    except subprocess.CalledProcessError as exc:
        _die(f"xcrun --show-sdk-path failed (exit {exc.returncode})")
    except FileNotFoundError:
        _die(f"executable not found: {_XCRUN!r}")


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
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(ns: argparse.Namespace, src_dir: Path) -> None:
    # -- signing identity: must be exactly 40 hex chars (fullmatch).
    if not _SHA1_RE.fullmatch(ns.signing_identity):
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

    # -- ALL six sources must be present as regular files before we start.
    missing = [
        name
        for name in _REQUIRED_SOURCES
        if not (src_dir / name).is_file()
    ]
    if missing:
        _die(
            "Missing required source file(s):\n"
            + "\n".join(f"  {src_dir / name}" for name in missing)
        )


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
        _CODESIGN,
        "--sign", signing_identity,
        "--identifier", identifier,
        "--timestamp=none",
        "--force",
    ]
    if signing_keychain:
        cmd += ["--keychain", signing_keychain]
    cmd.append(str(path))
    return cmd


def _verify_cmd(path: Path, identifier: str) -> list[str]:
    # Verify --strict with an exact requirement: designated identifier equals
    # the known ID, and the signing leaf certificate is the codesigning anchor.
    requirement = (
        f'identifier "{identifier}" and certificate leaf[subject.CN] exists'
    )
    return [
        _CODESIGN,
        "--verify",
        "--strict",
        "-R", requirement,
        str(path),
    ]


def build(ns: argparse.Namespace, src_dir: Path) -> None:
    out_dir = Path(ns.output_dir).resolve()
    signing_id = ns.signing_identity.upper()

    # Resolve SDK once: filesystem path → pass as -isysroot / -sdk.
    sdk = _sdk_path()
    print(f"[sdk] {sdk}")

    # Create a unique temporary directory as a sibling of out_dir so that
    # the final renamex_np rename is atomic (same filesystem).  We own this
    # directory; clean it on any failure path.
    with tempfile.TemporaryDirectory(
        dir=out_dir.parent,
        prefix=".build_tmp_",
    ) as _tmp_root:
        tmp_root = Path(_tmp_root)

        # Staging child: the directory we will atomically rename to out_dir.
        staging = tmp_root / "staging"
        staging.mkdir()

        try:
            _build_in(
                staging=staging,
                tmp_root=tmp_root,
                src_dir=src_dir,
                signing_id=signing_id,
                signing_keychain=ns.signing_keychain,
                sdk=sdk,
            )
        except SystemExit:
            # TemporaryDirectory.__exit__ cleans up tmp_root on any exit.
            raise

        # Atomically publish: rename staging → out_dir.
        # RENAME_EXCL fails if out_dir now exists (even as a broken symlink).
        try:
            _renamex_np(staging, out_dir)
        except OSError as exc:
            _die(f"atomic rename failed: {exc}\n  Destination: {out_dir}")

    print(f"Output written to: {out_dir}")
    print(f"  {out_dir / _HELPER_OUT}")
    print(f"  {out_dir / _DYLIB_OUT}")


def _build_in(
    *,
    staging: Path,
    tmp_root: Path,
    src_dir: Path,
    signing_id: str,
    signing_keychain: str | None,
    sdk: str,
) -> None:
    # Intermediate object files and module cache live under tmp_root (not staging).
    cache_dir = tmp_root / "obj"
    cache_dir.mkdir()
    module_cache = tmp_root / "module-cache"
    module_cache.mkdir()

    def src(name: str) -> Path:
        return src_dir / name

    # /usr/bin/xcrun wraps clang/swiftc; pass tool name only (not sdk name).
    clang_prefix = [_XCRUN, "clang"]
    swiftc_prefix = [_XCRUN, "swiftc"]

    target_flag = ["-target", _MACOS_TARGET]

    # Clang: C17, Blocks, full warnings, errors.
    c_compile_flags = [
        "-std=c17",
        "-fblocks",
        "-Wall",
        "-Wextra",
        "-Wpedantic",
        "-Werror",
        "-isysroot", sdk,
    ]

    # swiftc: Swift 5, library parse, explicit SDK filesystem path.
    swift_base_flags = [
        "-swift-version", "5",
        "-sdk", sdk,
        "-module-cache-path", str(module_cache),
        "-warnings-as-errors",
    ]

    # -----------------------------------------------------------------------
    # Compile C object files for the helper (main, listener, keychain)
    # -----------------------------------------------------------------------

    c_objects: list[Path] = []

    def compile_c(c_src: Path, extra: list[str] | None = None) -> Path:
        obj = cache_dir / (c_src.stem + ".o")
        cmd = (
            clang_prefix
            + target_flag
            + c_compile_flags
            + ["-c", str(c_src), "-o", str(obj)]
            + (extra or [])
        )
        print(f"[clang] {c_src.name}")
        _run(cmd)
        return obj

    # _C_MAIN: inject signing cert SHA1 as a quoted C string macro.
    cert_define = f'-DDH_HUB_SIGNING_CERT_SHA1="{signing_id}"'
    c_objects.append(compile_c(src(_C_MAIN), [cert_define]))
    c_objects.append(compile_c(src(_C_LISTENER)))
    c_objects.append(compile_c(src(_C_KEYCHAIN)))

    # -----------------------------------------------------------------------
    # Compile both Swift sources as a single -emit-executable invocation.
    # Swift linker driver: use clang (ld-based) via -use-ld or just rely on
    # swiftc's default linker; for object-only compilation we use -c then link
    # via swiftc with explicit C objects to get the Swift runtime linked in.
    # -----------------------------------------------------------------------

    swift_srcs = [src(_SWIFT_INVENTORY), src(_SWIFT_FETCH)]

    # Compile Swift sources to a single object file.
    swift_obj = cache_dir / "swift_combined.o"
    swift_compile_cmd = (
        swiftc_prefix
        + swift_base_flags
        + target_flag
        + [
            "-c",
            "-parse-as-library",
            "-module-name", "HubHelper",
        ]
        + [str(s) for s in swift_srcs]
        + ["-o", str(swift_obj)]
    )
    print(f"[swiftc] {' '.join(s.name for s in swift_srcs)}")
    _run(swift_compile_cmd)

    # -----------------------------------------------------------------------
    # Link the helper executable via swiftc (brings in Swift runtime libs).
    # Pass compiled C objects so they are included.  Use -use-ld=lld or leave
    # swiftc to choose; pass frameworks via -Xlinker.
    # -----------------------------------------------------------------------

    helper_path = staging / _HELPER_OUT

    link_fw: list[str] = []
    for fw in _FRAMEWORKS:
        link_fw += ["-Xlinker", "-framework", "-Xlinker", fw]

    swift_link_cmd = (
        swiftc_prefix
        + swift_base_flags
        + target_flag
        + ["-emit-executable"]
        + [str(swift_obj)]
        + [str(o) for o in c_objects]
        + link_fw
        + ["-o", str(helper_path)]
    )
    print(f"[link] {_HELPER_OUT}")
    _run(swift_link_cmd)

    # -----------------------------------------------------------------------
    # Build the XPC client shared dylib (clang -dynamiclib).
    # -----------------------------------------------------------------------

    dylib_obj = cache_dir / "macos_hub_xpc_client.o"
    cmd = (
        clang_prefix
        + target_flag
        + c_compile_flags
        + ["-c", str(src(_C_XPC_CLIENT)), "-o", str(dylib_obj)]
    )
    print(f"[clang] {_C_XPC_CLIENT}")
    _run(cmd)

    dylib_path = staging / _DYLIB_OUT
    cmd = (
        clang_prefix
        + target_flag
        + ["-dynamiclib"]
        + ["-isysroot", sdk]
        + ["-install_name", _DYLIB_INSTALL_NAME]
        + _framework_flags()
        + [str(dylib_obj)]
        + ["-o", str(dylib_path)]
    )
    print(f"[link dylib] {_DYLIB_OUT}")
    _run(cmd)

    # -----------------------------------------------------------------------
    # Sign both outputs — local cert, no timestamp, no hardened runtime.
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
    # Verify — strict, exact identifier + leaf requirement, both artifacts.
    # -----------------------------------------------------------------------

    print(f"[verify] {_HELPER_OUT}")
    _run(_verify_cmd(helper_path, _HELPER_ID))

    print(f"[verify] {_DYLIB_OUT}")
    _run(_verify_cmd(dylib_path, _DYLIB_ID))


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
