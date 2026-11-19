"""
Unit tests for scripts/snapshot_desktop_journal.py.

Covers:
- Successful snapshot creation in WAL mode
- Committed WAL with open writer connection
- Standalone reading of the snapshot without WAL/SHM
- Privacy protection: row counts only, no result, serial, or token leakage
- Missing source file error handling
- Matching source and output paths error handling
- Corrupted source database error handling
- Foreign/invalid schema (missing runs or run_targets table, missing required columns)
- True schema compatibility with extra fields (no artificial migration requirements)
- Prohibition of overwrite (existing regular file, broken symlink)
- Race condition handling during atomic link
- Staging file permissions (0600) and cleanup of own staging files only on failure
- Unicode and URI-special characters in paths (spaces, #, ?, %, Russian characters)
- CLI execution in text and JSON modes
- Help message explaining SQLite WAL and SHM requirements
- Timeout limit on backup progress and busy wait
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.snapshot_desktop_journal import (
    DatabaseCorruptError,
    OutputExistsError,
    SchemaValidationError,
    SnapshotError,
    SnapshotPermissionError,
    SnapshotTimeoutError,
    SourceNotFoundError,
    SourceSameAsOutputError,
    create_snapshot,
    main,
    make_sqlite_ro_uri,
)


def _init_wal_db(path: Path) -> sqlite3.Connection:
    """Initialize a valid DroidHub Desktop journal database in WAL mode."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE runs (
            run_id        TEXT PRIMARY KEY,
            scenario      TEXT NOT NULL,
            request_id    TEXT NOT NULL,
            params        TEXT NOT NULL,
            parent_run_id TEXT,
            status        TEXT NOT NULL DEFAULT 'queued',
            created_at    INTEGER NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE run_targets (
            run_id    TEXT NOT NULL REFERENCES runs(run_id),
            device_id TEXT NOT NULL,
            ord       INTEGER NOT NULL,
            status    TEXT NOT NULL DEFAULT 'queued',
            result    TEXT,
            PRIMARY KEY (run_id, device_id)
        );
    """)
    conn.commit()
    return conn


class TestSnapshotDesktopJournal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_snapshot_")
        self.tmp_path = Path(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_help_epilog_explains_wal_shm_requirements(self) -> None:
        """--help must explain SQLite requirements for WAL/SHM access."""
        res = subprocess.run(
            [sys.executable, "scripts/snapshot_desktop_journal.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res.returncode, 0)
        out = res.stdout
        self.assertIn("SQLite WAL / SHM Requirements:", out)
        self.assertIn("mode=ro", out)
        self.assertIn("WAL file", out)
        self.assertIn("shared-memory file (<db>-shm)", out)
        self.assertIn("committed", out)
        self.assertIn("DELETE", out)

    def test_successful_snapshot_and_permissions(self) -> None:
        """Create a consistent snapshot and check 0600 permissions."""
        src = self.tmp_path / "source.db"
        dst = self.tmp_path / "snapshot.db"

        with _init_wal_db(src) as conn:
            conn.execute(
                "INSERT INTO runs VALUES ('r1', 'sc1', 'req1', '{}', NULL, 'queued', 1000)"
            )
            conn.execute(
                "INSERT INTO run_targets VALUES ('r1', 'dev1', 0, 'queued', NULL)"
            )
            conn.commit()

        summary = create_snapshot(src, dst)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["runs_count"], 1)
        self.assertEqual(summary["run_targets_count"], 1)
        self.assertTrue(dst.is_file())

        # Check permissions: 0600
        mode = stat.S_IMODE(dst.stat().st_mode)
        self.assertEqual(mode, 0o600)

        # Check that no staging files remain
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_committed_wal_with_open_writer(self) -> None:
        """Committed WAL records must be captured while a writer remains actively open."""
        src = self.tmp_path / "source_live.db"
        dst = self.tmp_path / "snapshot_live.db"

        # Open writer connection and keep it open
        writer_conn = _init_wal_db(src)
        try:
            writer_conn.execute(
                "INSERT INTO runs VALUES ('r1', 'sc_live', 'req_live', '{}', NULL, 'succeeded', 2000)"
            )
            writer_conn.execute(
                "INSERT INTO run_targets VALUES ('r1', 'dev_live1', 0, 'succeeded', '{\"status\":\"ok\"}')"
            )
            writer_conn.execute(
                "INSERT INTO run_targets VALUES ('r1', 'dev_live2', 1, 'succeeded', '{\"status\":\"ok\"}')"
            )
            writer_conn.commit()

            # Ensure -wal file exists and contains uncheckpointed data
            wal_file = Path(str(src) + "-wal")
            self.assertTrue(wal_file.exists())

            # Perform snapshot while writer connection is still open
            summary = create_snapshot(src, dst)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["runs_count"], 1)
            self.assertEqual(summary["run_targets_count"], 2)

            # Check that the snapshot contains the committed WAL data
            with sqlite3.connect(str(dst)) as check_conn:
                rows = check_conn.execute("SELECT run_id, scenario, status FROM runs").fetchall()
                self.assertEqual(rows, [("r1", "sc_live", "succeeded")])
                targets = check_conn.execute("SELECT device_id, status FROM run_targets").fetchall()
                self.assertEqual(len(targets), 2)

            # Verify writer can continue writing without disruption
            writer_conn.execute(
                "INSERT INTO runs VALUES ('r2', 'sc_live2', 'req_live2', '{}', NULL, 'queued', 3000)"
            )
            writer_conn.commit()
            count = writer_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            writer_conn.close()

    def test_standalone_read_without_wal(self) -> None:
        """The snapshot must be self-contained in DELETE journal mode without needing WAL/SHM."""
        src = self.tmp_path / "src_standalone.db"
        dst = self.tmp_path / "dst_standalone.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 'sc', 'req', '{}', NULL, 'succeeded', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'succeeded', '{}')")
            conn.commit()

        create_snapshot(src, dst)

        # Move snapshot to a completely clean, isolated directory
        isolated_dir = Path(tempfile.mkdtemp(prefix="isolated_snap_"))
        try:
            isolated_file = isolated_dir / "isolated.db"
            shutil.copyfile(dst, isolated_file)

            # Verify no -wal or -shm files exist in isolated dir
            self.assertEqual(list(isolated_dir.iterdir()), [isolated_file])

            # Open with read-only connection
            ro_uri = make_sqlite_ro_uri(isolated_file)
            with sqlite3.connect(ro_uri, uri=True) as ro_conn:
                # Check PRAGMA journal_mode is delete, not wal
                cur = ro_conn.execute("PRAGMA journal_mode")
                mode = cur.fetchone()[0].lower()
                self.assertEqual(mode, "delete")

                # Verify reading records works
                runs = ro_conn.execute("SELECT run_id, scenario FROM runs").fetchall()
                self.assertEqual(runs, [("r1", "sc")])

            # Verify that reading did NOT create any -wal or -shm files
            files_after_read = sorted(f.name for f in isolated_dir.iterdir())
            self.assertEqual(files_after_read, ["isolated.db"])
        finally:
            shutil.rmtree(isolated_dir, ignore_errors=True)

    def test_privacy_no_device_serial_or_result_leak(self) -> None:
        """Output must only report row counts, never leaking serials, tokens, or result JSON."""
        src = self.tmp_path / "privacy.db"
        dst = self.tmp_path / "privacy_out.db"

        secret_serial = "DEVICE-SERIAL-SECRET-998877"
        secret_token = "AUTH_TOKEN_SECRET_XYZ"
        secret_result = json.dumps({"token": secret_token, "score": 100})

        with _init_wal_db(src) as conn:
            conn.execute(
                "INSERT INTO runs VALUES ('run-secret', 'smoke', 'req-1', '{}', NULL, 'succeeded', 100)"
            )
            conn.execute(
                "INSERT INTO run_targets VALUES ('run-secret', ?, 0, 'succeeded', ?)",
                (secret_serial, secret_result),
            )
            conn.commit()

        # Run CLI in text mode
        res_text = subprocess.run(
            [
                sys.executable,
                "scripts/snapshot_desktop_journal.py",
                "--source",
                str(src),
                "--output",
                str(dst),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res_text.returncode, 0)
        self.assertIn("Runs count:         1", res_text.stdout)
        self.assertIn("Run targets count:  1", res_text.stdout)
        self.assertNotIn(secret_serial, res_text.stdout)
        self.assertNotIn(secret_token, res_text.stdout)
        self.assertNotIn(secret_result, res_text.stdout)

        # Run CLI in JSON mode to another destination
        dst_json = self.tmp_path / "privacy_out_json.db"
        res_json = subprocess.run(
            [
                sys.executable,
                "scripts/snapshot_desktop_journal.py",
                "--source",
                str(src),
                "--output",
                str(dst_json),
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res_json.returncode, 0)
        parsed = json.loads(res_json.stdout)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["runs_count"], 1)
        self.assertEqual(parsed["run_targets_count"], 1)
        self.assertNotIn(secret_serial, res_json.stdout)
        self.assertNotIn(secret_token, res_json.stdout)
        self.assertNotIn(secret_result, res_json.stdout)

    def test_missing_source_fails_and_leaves_no_file(self) -> None:
        """Missing source file must raise SourceNotFoundError and leave no destination file."""
        src = self.tmp_path / "nonexistent.db"
        dst = self.tmp_path / "never_created.db"

        with self.assertRaises(SourceNotFoundError):
            create_snapshot(src, dst)
        self.assertFalse(dst.exists())

        # CLI test
        res = subprocess.run(
            [
                sys.executable,
                "scripts/snapshot_desktop_journal.py",
                "--source",
                str(src),
                "--output",
                str(dst),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not exist", res.stderr)
        self.assertFalse(dst.exists())

    def test_same_source_and_output_fails(self) -> None:
        """Source and output pointing to the same file must be rejected."""
        src = self.tmp_path / "same.db"
        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 'sc', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        with self.assertRaises(SourceSameAsOutputError):
            create_snapshot(src, src)

        # CLI test
        res = subprocess.run(
            [
                sys.executable,
                "scripts/snapshot_desktop_journal.py",
                "--source",
                str(src),
                "--output",
                str(src),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("same file", res.stderr)

    def test_corrupted_source_fails_and_cleans_up(self) -> None:
        """Corrupted source database must raise DatabaseCorruptError and clean up staging."""
        src = self.tmp_path / "corrupt.db"
        dst = self.tmp_path / "corrupt_out.db"

        src.write_bytes(b"THIS IS NOT A SQLITE DATABASE")

        with self.assertRaises(DatabaseCorruptError):
            create_snapshot(src, dst)

        self.assertFalse(dst.exists())
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_schema_missing_runs_table(self) -> None:
        """Database missing 'runs' table must raise SchemaValidationError."""
        src = self.tmp_path / "no_runs.db"
        dst = self.tmp_path / "no_runs_out.db"

        with sqlite3.connect(str(src)) as conn:
            conn.execute("""
                CREATE TABLE run_targets (
                    run_id TEXT, device_id TEXT, status TEXT, result TEXT
                )
            """)
            conn.commit()

        with self.assertRaises(SchemaValidationError):
            create_snapshot(src, dst)

        self.assertFalse(dst.exists())
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_schema_missing_run_targets_table(self) -> None:
        """Database missing 'run_targets' table must raise SchemaValidationError."""
        src = self.tmp_path / "no_targets.db"
        dst = self.tmp_path / "no_targets_out.db"

        with sqlite3.connect(str(src)) as conn:
            conn.execute("""
                CREATE TABLE runs (
                    run_id TEXT, scenario TEXT, status TEXT, created_at INTEGER
                )
            """)
            conn.commit()

        with self.assertRaises(SchemaValidationError):
            create_snapshot(src, dst)

        self.assertFalse(dst.exists())

    def test_schema_missing_required_column(self) -> None:
        """Table missing required column must raise SchemaValidationError."""
        src = self.tmp_path / "missing_col.db"
        dst = self.tmp_path / "missing_col_out.db"

        with sqlite3.connect(str(src)) as conn:
            # runs missing created_at
            conn.execute("CREATE TABLE runs (run_id TEXT, scenario TEXT, status TEXT)")
            conn.execute("CREATE TABLE run_targets (run_id TEXT, device_id TEXT, status TEXT, result TEXT)")
            conn.commit()

        with self.assertRaises(SchemaValidationError):
            create_snapshot(src, dst)

        self.assertFalse(dst.exists())

    def test_schema_with_extra_columns_succeeds(self) -> None:
        """Schema with extra columns (true desktop schema) must succeed without extra demands."""
        src = self.tmp_path / "extra_cols.db"
        dst = self.tmp_path / "extra_cols_out.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req1', '{\"p\":1}', 'parent', 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        summary = create_snapshot(src, dst)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["runs_count"], 1)
        self.assertEqual(summary["run_targets_count"], 1)

    def test_prohibition_of_overwrite_existing_file(self) -> None:
        """Existing destination file must not be overwritten."""
        src = self.tmp_path / "src_no_overwrite.db"
        dst = self.tmp_path / "dst_no_overwrite.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        # Create destination ahead of time with sensitive existing content
        dst.write_text("EXISTING IMPORTANT FILE CONTENT")

        with self.assertRaises(OutputExistsError):
            create_snapshot(src, dst)

        # Confirm content was not modified
        self.assertEqual(dst.read_text(), "EXISTING IMPORTANT FILE CONTENT")

        # Staging file cleaned up
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_prohibition_of_overwrite_broken_symlink(self) -> None:
        """Broken symlink at destination must not be overwritten."""
        src = self.tmp_path / "src_symlink.db"
        dst = self.tmp_path / "dst_broken_symlink.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        # Create broken symlink
        os.symlink(self.tmp_path / "non_existent_target", dst)
        self.assertTrue(os.path.islink(dst))
        self.assertFalse(dst.exists())
        self.assertTrue(os.path.lexists(dst))

        with self.assertRaises(OutputExistsError):
            create_snapshot(src, dst)

        # Broken symlink must still be a symlink and point to nonexistent target
        self.assertTrue(os.path.islink(dst))
        self.assertFalse(dst.exists())

    def test_race_condition_on_link(self) -> None:
        """If another process creates the target right before link, link collision is safely caught."""
        src = self.tmp_path / "src_race.db"
        dst = self.tmp_path / "dst_race.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        # Mock os.link to raise FileExistsError (simulating race)
        with patch("os.link", side_effect=FileExistsError("Simulated race collision")):
            with self.assertRaises(OutputExistsError):
                create_snapshot(src, dst)

        # Staging file cleaned up
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_staging_cleanup_on_failure_leaves_other_files_intact(self) -> None:
        """On failure, only our own staging files must be removed, leaving other files alone."""
        src = self.tmp_path / "src_clean.db"
        dst = self.tmp_path / "dst_clean.db"

        # Other files in destination directory
        other_file = self.tmp_path / "other_important_file.txt"
        other_file.write_text("DO NOT TOUCH")

        # Corrupt source to trigger failure
        src.write_bytes(b"CORRUPT")

        with self.assertRaises(DatabaseCorruptError):
            create_snapshot(src, dst)

        self.assertTrue(other_file.is_file())
        self.assertEqual(other_file.read_text(), "DO NOT TOUCH")
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_unicode_and_uri_special_chars_in_paths(self) -> None:
        """Paths with Unicode, spaces, ?, #, %, and brackets must be fully supported."""
        special_dir = self.tmp_path / "тест папка #1 ? %20 [v1.0]"
        special_dir.mkdir()

        src = special_dir / "исходник журнал #тест ? 100%.db"
        dst = special_dir / "снимок журнал #тест ? 100%.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 'тест_сценарий', 'req1', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'устройство-1', 0, 'queued', NULL)")
            conn.commit()

        summary = create_snapshot(src, dst)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["runs_count"], 1)
        self.assertEqual(summary["run_targets_count"], 1)
        self.assertTrue(dst.is_file())

        # Verify data read back from unicode path
        with sqlite3.connect(make_sqlite_ro_uri(dst), uri=True) as ro_conn:
            rows = ro_conn.execute("SELECT run_id, scenario FROM runs").fetchall()
            self.assertEqual(rows, [("r1", "тест_сценарий")])

    def test_timeout_monitoring_and_limit(self) -> None:
        """Exceeding timeout during backup progress raises SnapshotTimeoutError and cleans up."""
        src = self.tmp_path / "src_timeout.db"
        dst = self.tmp_path / "dst_timeout.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        # Call with timeout = -1.0 so time.monotonic() - start_time > timeout triggers immediately
        with self.assertRaises(SnapshotTimeoutError):
            create_snapshot(src, dst, timeout=-1.0, pages_per_step=1)

        self.assertFalse(dst.exists())
        staging_files = [f for f in self.tmp_path.iterdir() if "staging" in f.name]
        self.assertEqual(staging_files, [])

    def test_cli_json_and_text_error_output(self) -> None:
        """CLI errors must output clean JSON with ok:false in --json mode and exit non-zero."""
        src = self.tmp_path / "missing.db"
        dst = self.tmp_path / "out.db"

        res = subprocess.run(
            [
                sys.executable,
                "scripts/snapshot_desktop_journal.py",
                "--source",
                str(src),
                "--output",
                str(dst),
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res.returncode, 1)
        parsed = json.loads(res.stdout)
        self.assertFalse(parsed["ok"])
        self.assertIn("error", parsed)

    def test_permission_error_unreadable_source(self) -> None:
        """Unreadable source file must raise SnapshotPermissionError and non-zero exit in CLI."""
        src = self.tmp_path / "unreadable.db"
        dst = self.tmp_path / "unreadable_out.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        os.chmod(src, 0o000)
        try:
            with self.assertRaises(SnapshotPermissionError):
                create_snapshot(src, dst)

            # Test CLI
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/snapshot_desktop_journal.py",
                    "--source",
                    str(src),
                    "--output",
                    str(dst),
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Permission denied", res.stderr)
        finally:
            os.chmod(src, 0o600)

    def test_permission_error_unwritable_destination_dir(self) -> None:
        """Unwritable destination directory must raise SnapshotPermissionError and non-zero exit in CLI."""
        src = self.tmp_path / "src_ro_dir.db"
        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        ro_dir = self.tmp_path / "ro_dest_dir"
        ro_dir.mkdir()
        os.chmod(ro_dir, 0o400)
        dst = ro_dir / "snapshot.db"

        try:
            with self.assertRaises(SnapshotPermissionError):
                create_snapshot(src, dst)

            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/snapshot_desktop_journal.py",
                    "--source",
                    str(src),
                    "--output",
                    str(dst),
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Permission denied", res.stderr)
        finally:
            os.chmod(ro_dir, 0o700)

    def test_source_remains_unmodified_no_checkpoint_or_writes(self) -> None:
        """Source database and WAL file must not be modified or checkpointed during snapshot."""
        src = self.tmp_path / "src_readonly_check.db"
        dst = self.tmp_path / "dst_readonly_check.db"

        writer = _init_wal_db(src)
        try:
            writer.execute("INSERT INTO runs VALUES ('r1', 'sc', 'req', '{}', NULL, 'queued', 100)")
            writer.execute("INSERT INTO run_targets VALUES ('r1', 'dev1', 0, 'queued', NULL)")
            writer.commit()

            wal_file = Path(str(src) + "-wal")
            self.assertTrue(wal_file.exists())
            wal_size_before = wal_file.stat().st_size
            db_size_before = src.stat().st_size

            # Run snapshot
            create_snapshot(src, dst)

            # Check that WAL file still exists and size has not decreased to 0 (no checkpoint)
            self.assertTrue(wal_file.exists())
            self.assertEqual(wal_file.stat().st_size, wal_size_before)
            self.assertEqual(src.stat().st_size, db_size_before)
        finally:
            writer.close()

    def test_cli_text_success_output(self) -> None:
        """CLI text output must contain formatted statistics and zero exit code."""
        src = self.tmp_path / "cli_text.db"
        dst = self.tmp_path / "cli_text_out.db"

        with _init_wal_db(src) as conn:
            conn.execute("INSERT INTO runs VALUES ('r1', 'sc', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'dev1', 0, 'queued', NULL)")
            conn.commit()

        res = subprocess.run(
            [
                sys.executable,
                "scripts/snapshot_desktop_journal.py",
                "-s",
                str(src),
                "-o",
                str(dst),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Snapshot created successfully:", res.stdout)
        self.assertIn("Runs count:         1", res.stdout)
        self.assertIn("Run targets count:  1", res.stdout)
        self.assertIn("Size (bytes):", res.stdout)

    def test_cli_missing_required_args(self) -> None:
        """CLI missing required arguments must exit with code 2."""
        res = subprocess.run(
            [sys.executable, "scripts/snapshot_desktop_journal.py"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res.returncode, 2)

    def test_make_sqlite_ro_uri_encoding(self) -> None:
        """make_sqlite_ro_uri must create proper mode=ro URI without immutable=1 and encode symbols."""
        p = self.tmp_path / "бд #1 ? 100% [test]"
        uri = make_sqlite_ro_uri(p)
        self.assertTrue(uri.startswith("file:///"))
        self.assertTrue(uri.endswith("?mode=ro"))
        self.assertNotIn("immutable=1", uri)
        self.assertIn("%231", uri)  # # encoded as %23
        self.assertIn("%3F", uri)   # ? encoded as %3F
        self.assertIn("%25", uri)   # % encoded as %25

    def test_schema_version_mismatch_with_desktop_schema(self) -> None:
        """If schema_meta has incompatible version and desktop is importable, SchemaValidationError is raised."""
        src = self.tmp_path / "bad_ver.db"
        dst = self.tmp_path / "bad_ver_out.db"

        with _init_wal_db(src) as conn:
            conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '999')")
            conn.execute("INSERT INTO runs VALUES ('r1', 's', 'req', '{}', NULL, 'queued', 100)")
            conn.execute("INSERT INTO run_targets VALUES ('r1', 'd1', 0, 'queued', NULL)")
            conn.commit()

        # Mock desktop.run_store.SCHEMA_VERSION = 1
        dummy_mod = type(sys)("desktop.run_store")
        dummy_mod.SCHEMA_VERSION = 1
        with patch.dict(sys.modules, {"desktop": type(sys)("desktop"), "desktop.run_store": dummy_mod}):
            with self.assertRaises(SchemaValidationError):
                create_snapshot(src, dst)


if __name__ == "__main__":
    unittest.main()
