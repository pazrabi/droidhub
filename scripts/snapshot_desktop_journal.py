#!/usr/bin/env python3
"""
Safely create a consistent standalone snapshot of DroidHub Desktop SQLite journal.

Standalone CLI tool using only the Python standard library.
Enforces read-only access with SQLite URI mode=ro (not immutable, capturing committed WAL),
performs online backup, validates integrity and schema in a staging file (0600),
converts the snapshot to a self-contained non-WAL database, and atomically publishes
via hard link without overwriting existing files or broken symlinks.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sqlite3
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Required schema contracts
REQUIRED_RUNS_COLUMNS = frozenset({"run_id", "scenario", "status", "created_at"})
REQUIRED_RUN_TARGETS_COLUMNS = frozenset({"run_id", "device_id", "status", "result"})


class SnapshotError(Exception):
    """Base exception for snapshot operations."""


class SourceNotFoundError(SnapshotError):
    """Source database path does not exist or is not a regular file."""


class SourceSameAsOutputError(SnapshotError):
    """Source and output point to the same file."""


class OutputExistsError(SnapshotError):
    """Output destination already exists (refusing to overwrite)."""


class DatabaseCorruptError(SnapshotError):
    """Database failed integrity check or is corrupted."""


class SchemaValidationError(SnapshotError):
    """Database schema does not match required contract."""


class SnapshotTimeoutError(SnapshotError):
    """Backup or busy wait exceeded timeout limit."""


class SnapshotPermissionError(SnapshotError):
    """Permission denied accessing source, staging, or destination."""


def make_sqlite_ro_uri(path: str | Path) -> str:
    """Return a sqlite3 URI in read-only mode (mode=ro) with proper quoting.

    Path.resolve().as_uri() safely percent-encodes spaces, ?, #, %, and unicode characters.
    Appending '?mode=ro' tells SQLite to open in read-only mode without modifying files.
    We explicitly avoid 'immutable=1' so SQLite opens the WAL file and sees committed transactions.
    """
    resolved = Path(path).resolve()
    base_uri = resolved.as_uri()
    return f"{base_uri}?mode=ro"


def _cleanup_staging(staging_path: Path) -> None:
    """Safely remove only the staging file and its auxiliary files."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        aux = Path(str(staging_path) + suffix)
        try:
            if os.path.lexists(aux):
                os.unlink(aux)
        except OSError:
            pass


def _verify_real_schema_if_available(conn: sqlite3.Connection, existing_tables: set[str]) -> None:
    """Verify against real Desktop schema if desktop module is available.

    Does not invent migrations or require unnecessary fields beyond the contract.
    """
    try:
        from desktop.run_store import SCHEMA_VERSION as DESKTOP_SCHEMA_VERSION
        if "schema_meta" in existing_tables:
            cur = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is not None:
                stored = int(row[0])
                if stored != DESKTOP_SCHEMA_VERSION:
                    raise SchemaValidationError(
                        f"Unsupported schema version {stored}; expected {DESKTOP_SCHEMA_VERSION}"
                    )
    except ImportError:
        pass


def create_snapshot(
    source: str | Path,
    output: str | Path,
    *,
    timeout: float = 30.0,
    pages_per_step: int = 256,
) -> dict[str, Any]:
    """Create a consistent, standalone SQLite snapshot from source to output.

    Parameters
    ----------
    source:
        Path to existing SQLite source database.
    output:
        Path to destination snapshot file. Must not already exist.
    timeout:
        Timeout in seconds for busy locks and backup progress monitoring.
    pages_per_step:
        Pages to transfer per online backup iteration.

    Returns
    -------
    Dictionary with summary containing only row counts and metadata:
      - ok: True
      - source: absolute path to source
      - output: absolute path to destination
      - runs_count: number of rows in runs table
      - run_targets_count: number of rows in run_targets table
      - size_bytes: size of destination file in bytes
    """
    try:
        src_path = Path(source).resolve()
        raw_out = Path(output)
        out_dir = raw_out.parent.resolve()
        out_path = out_dir / raw_out.name

        # 1. Validate destination directory exists and is writable
        if not out_dir.exists():
            raise SnapshotError(f"Output directory does not exist: {out_dir}")
        if not out_dir.is_dir():
            raise SnapshotError(f"Output directory path is not a directory: {out_dir}")
        if not os.access(out_dir, os.W_OK | os.X_OK):
            raise SnapshotPermissionError(f"Permission denied writing to output directory: {out_dir}")

        # 2. Validate source existence and permissions
        if not src_path.exists() or not src_path.is_file():
            raise SourceNotFoundError(f"Source database does not exist or is not a regular file: {source}")

        if not os.access(src_path, os.R_OK):
            raise SnapshotPermissionError(f"Permission denied reading source database: {source}")

        # 3. Check source and output are not the same file
        if src_path == out_path or (out_path.exists() and src_path.samefile(out_path)):
            raise SourceSameAsOutputError(f"Source and output paths point to the same file: {src_path}")

        # 4. Check that output destination does not already exist (including broken symlinks)
        if os.path.lexists(out_path) or os.path.lexists(raw_out):
            raise OutputExistsError(f"Output destination already exists: {out_path}")
    except PermissionError as exc:
        raise SnapshotPermissionError(f"Permission denied accessing paths: {exc}") from exc

    # 5. Create staging file in destination directory with permissions 0600
    staging_name = f".{out_path.name}.staging.{uuid.uuid4().hex}.tmp"
    staging_path = out_dir / staging_name
    staging_created = False

    try:
        try:
            fd = os.open(staging_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(staging_path, 0o600)
            staging_created = True
        except PermissionError as exc:
            raise SnapshotPermissionError(
                f"Permission denied creating staging file in {out_dir}: {exc}"
            ) from exc

        # 6. Open source in read-only mode using URI mode=ro (no write SQL, checkpoint, or repair)
        src_uri = make_sqlite_ro_uri(src_path)
        try:
            src_conn = sqlite3.connect(src_uri, uri=True, timeout=timeout)
        except sqlite3.OperationalError as exc:
            err_msg = str(exc).lower()
            if "permission denied" in err_msg or "readonly" in err_msg:
                raise SnapshotPermissionError(
                    f"Permission denied opening source database: {exc}"
                ) from exc
            raise SnapshotError(f"Unable to open source database: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptError(f"Source database file is corrupted or not a SQLite DB: {exc}") from exc

        # Verify source is readable
        try:
            cur = src_conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            cur.fetchone()
        except sqlite3.DatabaseError as exc:
            src_conn.close()
            raise DatabaseCorruptError(f"Source database corrupted: {exc}") from exc

        # 7. Connect to staging database and perform online backup with progress/timeout limits
        dst_conn = sqlite3.connect(str(staging_path), timeout=timeout)
        start_time = time.monotonic()

        def progress_callback(status: int, remaining: int, total: int) -> None:
            if time.monotonic() - start_time > timeout:
                raise SnapshotTimeoutError(
                    f"Backup progress exceeded timeout limit ({timeout:.1f}s), "
                    f"remaining pages: {remaining}/{total}"
                )

        try:
            src_conn.backup(dst_conn, pages=pages_per_step, progress=progress_callback, sleep=0.05)
        except SnapshotTimeoutError:
            raise
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "busy" in msg or "locked" in msg:
                raise SnapshotTimeoutError(f"Database locked or busy during online backup: {exc}") from exc
            if "corrupt" in msg or "malformed" in msg:
                raise DatabaseCorruptError(f"Database corruption encountered during backup: {exc}") from exc
            raise SnapshotError(f"Operational error during online backup: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptError(f"Database error during online backup: {exc}") from exc
        finally:
            src_conn.close()

        # 8. In staging file: run PRAGMA quick_check
        try:
            cur = dst_conn.execute("PRAGMA quick_check")
            check_rows = cur.fetchall()
            if not check_rows or len(check_rows) != 1 or check_rows[0][0] != "ok":
                errors = [r[0] for r in check_rows]
                raise DatabaseCorruptError(f"PRAGMA quick_check integrity check failed: {errors}")
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptError(f"Integrity check failed with database error: {exc}") from exc

        # 9. Verify expected schema: tables runs and run_targets
        cur = dst_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cur.fetchall()}

        if "runs" not in existing_tables:
            raise SchemaValidationError("Required table 'runs' is missing from database schema")
        if "run_targets" not in existing_tables:
            raise SchemaValidationError("Required table 'run_targets' is missing from database schema")

        # Validate required columns in 'runs'
        cur = dst_conn.execute("PRAGMA table_info(runs)")
        runs_cols = {row[1] for row in cur.fetchall()}
        missing_runs = REQUIRED_RUNS_COLUMNS - runs_cols
        if missing_runs:
            raise SchemaValidationError(
                f"Table 'runs' missing required column(s): {sorted(missing_runs)}"
            )

        # Validate required columns in 'run_targets'
        cur = dst_conn.execute("PRAGMA table_info(run_targets)")
        targets_cols = {row[1] for row in cur.fetchall()}
        missing_targets = REQUIRED_RUN_TARGETS_COLUMNS - targets_cols
        if missing_targets:
            raise SchemaValidationError(
                f"Table 'run_targets' missing required column(s): {sorted(missing_targets)}"
            )

        # Optional schema verification if desktop available
        _verify_real_schema_if_available(dst_conn, existing_tables)

        # 10. Compute row counts for privacy-safe reporting (no result, serials, or tokens fetched)
        cur = dst_conn.execute("SELECT COUNT(*) FROM runs")
        runs_count = cur.fetchone()[0]

        cur = dst_conn.execute("SELECT COUNT(*) FROM run_targets")
        run_targets_count = cur.fetchone()[0]

        # 11. Ensure database self-sufficiency without WAL (convert journal_mode to DELETE)
        dst_conn.execute("PRAGMA journal_mode = DELETE;")
        dst_conn.commit()
        dst_conn.close()

        # Clean up any auxiliary files created by staging if any
        for aux_suffix in ("-wal", "-shm", "-journal"):
            aux_file = Path(str(staging_path) + aux_suffix)
            if os.path.lexists(aux_file):
                try:
                    os.unlink(aux_file)
                except OSError:
                    pass

        # Verify standalone readability in read-only mode without WAL
        verify_uri = make_sqlite_ro_uri(staging_path)
        v_conn = sqlite3.connect(verify_uri, uri=True)
        try:
            v_cur = v_conn.execute("PRAGMA journal_mode")
            mode_name = v_cur.fetchone()[0].lower()
            if mode_name == "wal":
                raise SnapshotError(
                    f"Staging database journal mode is {mode_name}, expected non-WAL self-contained mode"
                )
            v_conn.execute("SELECT COUNT(*) FROM runs")
        finally:
            v_conn.close()

        # Enforce 0600 permissions
        os.chmod(staging_path, 0o600)
        size_bytes = staging_path.stat().st_size

        # 12. Atomically publish via hard link without overwriting existing files or broken symlinks
        if os.path.lexists(out_path):
            raise OutputExistsError(f"Output destination already exists: {out_path}")

        try:
            os.link(staging_path, out_path)
        except FileExistsError as exc:
            raise OutputExistsError(
                f"Output destination already exists (link collision): {out_path}"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise SnapshotError(
                    f"Cannot link across different filesystems: {staging_path} -> {out_path}"
                ) from exc
            raise SnapshotError(f"Failed to atomically link snapshot file: {exc}") from exc

        # Success! Staging file is now safely linked as out_path; remove staging link
        staging_created = False
        try:
            os.unlink(staging_path)
        except OSError:
            pass

        return {
            "ok": True,
            "source": str(src_path),
            "output": str(out_path),
            "runs_count": runs_count,
            "run_targets_count": run_targets_count,
            "size_bytes": size_bytes,
        }

    finally:
        # On error: delete only our staging files
        if staging_created:
            _cleanup_staging(staging_path)


def format_text_summary(summary: dict[str, Any]) -> str:
    """Format snapshot summary into clean text without secrets or serials."""
    lines = [
        "Snapshot created successfully:",
        f"  Source:             {summary['source']}",
        f"  Output:             {summary['output']}",
        f"  Runs count:         {summary['runs_count']}",
        f"  Run targets count:  {summary['run_targets_count']}",
        f"  Size (bytes):       {summary['size_bytes']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for snapshot_desktop_journal."""
    parser = argparse.ArgumentParser(
        description="Safely create a consistent standalone copy of DroidHub Desktop SQLite journal.",
        epilog="""
SQLite WAL / SHM Requirements:
When accessing a SQLite database operating in Write-Ahead Logging (WAL) mode with
mode=ro (read-only URI), SQLite still requires read access to the WAL file (<db>-wal)
and read-write access to the shared-memory file (<db>-shm) in order to coordinate
concurrency and read committed transactions. If permissions on <db>-wal, <db>-shm, or
the directory do not allow this, mode=ro cannot open the database.
Using mode=ro without immutable=1 ensures that all committed WAL transactions are
consistently captured via SQLite online backup without requiring writes, checkpoints,
or repair operations on the source database. The resulting snapshot is converted to
standard rollback mode (DELETE) to ensure it is completely self-contained without WAL.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        "-s",
        required=True,
        help="Path to source SQLite journal database file",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path to destination snapshot SQLite file (must not already exist)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output snapshot summary in JSON format",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for busy lock and backup progress (default: 30.0)",
    )

    args = parser.parse_args(argv)

    try:
        summary = create_snapshot(args.source, args.output, timeout=args.timeout)
        if args.as_json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(format_text_summary(summary))
        return 0
    except SnapshotError as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            sys.stderr.write(f"Error: {exc}\n")
        return 1
    except Exception as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": f"Unexpected error: {exc}"}, indent=2, sort_keys=True))
        else:
            sys.stderr.write(f"Unexpected error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
