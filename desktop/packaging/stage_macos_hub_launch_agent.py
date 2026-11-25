"""
Staging tool for the stable read-only macOS hub helper launchd agent.

Usage:
    python3 -m desktop.packaging.stage_macos_hub_launch_agent \\
        --output /path/to/fresh/site.molokohub.droidhub.hub-helper.v1.plist \\
        --helper /absolute/path/to/droidhub-hub-helper \\
        --signing-identity AABBCCDDEEFF00112233445566778899AABBCCDD

This script writes a launchd property list (.plist) for the stable read-only
helper tool to an explicit fresh --output path. It performs no actual install
or launchd bootstrap operations, never reads user configurations or credentials,
and only invokes /usr/bin/codesign to verify helper code identity.

Fixed Label and MachServices key:
    site.molokohub.droidhub.hub-helper.v1

ProgramArguments:
    [absolute helper path]

No RunAtLoad, KeepAlive, or runtime overrides are written.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LABEL = "site.molokohub.droidhub.hub-helper.v1"
_MACH_SERVICE = "site.molokohub.droidhub.hub-helper.v1"
_CODESIGN = "/usr/bin/codesign"

_SHA1_RE = re.compile(r"^[0-9A-Fa-f]{40}$")

LABEL = _LABEL
MACH_SERVICE = _MACH_SERVICE
HELPER_ID = _LABEL
CODESIGN = _CODESIGN


# ---------------------------------------------------------------------------
# Helper verification & staging
# ---------------------------------------------------------------------------


def stage_macos_hub_launch_agent(
    output: str | Path,
    helper: str | Path,
    signing_identity: str,
    *,
    codesign_bin: str = _CODESIGN,
) -> Path:
    """Stage launchd plist for the hub helper binary to a fresh output path.

    Parameters:
        output: Destination path for the generated plist (must not already exist).
        helper: Absolute path to the existing regular helper binary.
        signing_identity: 40-hex SHA-1 fingerprint of the signing certificate.
        codesign_bin: Path to the codesign binary (defaults to /usr/bin/codesign).

    Returns:
        Path to the successfully created plist file.
    """
    # ------------------------------------------------------------------
    # 1. Validate --helper path
    # ------------------------------------------------------------------
    helper_str = str(helper)
    if not helper_str:
        raise ValueError("--helper path must not be empty")

    if not os.path.isabs(helper_str):
        raise ValueError(f"--helper must be an absolute path: {helper_str!r}")

    # Refuse symlink helper (lexists / islink check before exists)
    if os.path.islink(helper_str) or Path(helper_str).is_symlink():
        raise ValueError(f"--helper must not be a symbolic link: {helper_str!r}")

    if not os.path.exists(helper_str):
        raise FileNotFoundError(f"--helper path does not exist: {helper_str!r}")

    if not os.path.isfile(helper_str):
        raise ValueError(f"--helper path must be a regular file: {helper_str!r}")

    helper_path = Path(helper_str)

    # ------------------------------------------------------------------
    # 2. Validate --signing-identity
    # ------------------------------------------------------------------
    if not isinstance(signing_identity, str) or not _SHA1_RE.fullmatch(signing_identity):
        raise ValueError(
            "--signing-identity must be exactly 40 hexadecimal characters "
            f"(got {signing_identity!r})"
        )

    # ------------------------------------------------------------------
    # 3. Validate --output path
    # ------------------------------------------------------------------
    output_str = str(output)
    if not output_str.strip():
        raise ValueError("--output path must not be empty")

    if os.path.lexists(output_str):
        raise FileExistsError(
            f"--output path already exists; refusing to overwrite: {output_str!r}"
        )

    output_path = Path(output_str)
    parent_dir = output_path.parent
    if not parent_dir.exists():
        raise FileNotFoundError(
            f"--output parent directory does not exist: {parent_dir!s}"
        )
    if not parent_dir.is_dir():
        raise NotADirectoryError(
            f"--output parent path is not a directory: {parent_dir!s}"
        )

    # ------------------------------------------------------------------
    # 4. Verify helper with codesign
    # ------------------------------------------------------------------
    req = f'=identifier "{_LABEL}" and certificate leaf = H"{signing_identity}"'
    cmd = [
        codesign_bin,
        "--verify",
        "--strict",
        "-R",
        req,
        str(helper_path),
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        err_msg = (exc.stderr or exc.stdout or "").strip() or f"exit code {exc.returncode}"
        raise RuntimeError(
            f"codesign verification failed for {helper_path.name} (exit {exc.returncode}): {err_msg}"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"executable not found: {codesign_bin!r}") from exc

    # ------------------------------------------------------------------
    # 5. Build launchd plist data
    # ------------------------------------------------------------------
    plist_data = {
        "Label": _LABEL,
        "MachServices": {
            _MACH_SERVICE: True,
        },
        "ProgramArguments": [str(helper_path)],
    }
    plist_bytes = plistlib.dumps(plist_data, fmt=plistlib.FMT_XML, sort_keys=True)

    # ------------------------------------------------------------------
    # 6. Exclusive file creation with cleanup on partial failure
    # ------------------------------------------------------------------
    created = False
    fd = -1
    try:
        fd = os.open(str(output_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        created = True
        with open(fd, "wb", closefd=True) as f:
            fd = -1  # open() took ownership of fd
            f.write(plist_bytes)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if created and os.path.lexists(str(output_path)):
            try:
                os.unlink(str(output_path))
            except OSError:
                pass
        raise

    return output_path


stage_launch_agent = stage_macos_hub_launch_agent


# ---------------------------------------------------------------------------
# CLI Argument parsing and entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m desktop.packaging.stage_macos_hub_launch_agent",
        description="Stage launchd plist for stable read-only macOS hub helper.",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        metavar="PATH",
        help="Explicit fresh output path for the launchd plist file.",
    )
    parser.add_argument(
        "--helper",
        required=True,
        metavar="PATH",
        help="Absolute path to the stable read-only hub helper binary.",
    )
    parser.add_argument(
        "--signing-identity",
        required=True,
        metavar="HEX40",
        help="40-hex SHA-1 certificate fingerprint of the signing identity.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    args = _parse_args(argv)

    try:
        stage_macos_hub_launch_agent(
            output=args.output,
            helper=args.helper,
            signing_identity=args.signing_identity,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
