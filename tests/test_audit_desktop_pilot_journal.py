"""Unit tests for scripts/audit_desktop_pilot_journal.py."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.audit_desktop_pilot_journal import (
    EXPECTED_SCENARIO,
    LIMITATION_NOTE,
    TERMINAL_STATUSES,
    audit,
    main,
)


class BaseAuditTest(unittest.TestCase):
    """Helper base class providing SQLite and JSONL fixture generation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_snapshot(
        self,
        rows: list[tuple[str, str, str]],
        filename: str = "snapshot.db",
        schema: str = "CREATE TABLE runs (run_id TEXT, scenario TEXT, status TEXT);",
    ) -> Path:
        db_path = self.dir_path / filename
        conn = sqlite3.connect(str(db_path))
        if schema:
            conn.executescript(schema)
        for r in rows:
            conn.execute("INSERT INTO runs VALUES (?, ?, ?)", r)
        conn.commit()
        conn.close()
        return db_path

    def create_events(self, events: list[dict], filename: str = "events.jsonl") -> Path:
        events_path = self.dir_path / filename
        with open(events_path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        return events_path


class TestMatchCases(BaseAuditTest):
    """Tests where pilot events and SQLite snapshot match completely (exit 0)."""

    def test_full_match_all_terminal_statuses(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "sample", "elapsed_sec": 0.5},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "disconnect", "elapsed_sec": 1.2},
            {"type": "reconnect", "elapsed_sec": 1.5},
            {"type": "run", "status": "failed", "elapsed_sec": 2.0},
            {"type": "run", "status": "partial", "elapsed_sec": 2.5},
            {"type": "run", "status": "cancelled", "elapsed_sec": 3.0},
            {"type": "sample", "elapsed_sec": 3.1},
            {"type": "shutdown", "elapsed_sec": 3.5},
        ]
        db_rows = [
            ("run_001", "usb_diagnostics", "succeeded"),
            ("run_002", "usb_diagnostics", "failed"),
            ("run_003", "usb_diagnostics", "partial"),
            ("run_004", "usb_diagnostics", "cancelled"),
        ]

        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 0)
        self.assertEqual(res["status"], "match")
        self.assertTrue(res["match"])
        self.assertEqual(res["scenario"], EXPECTED_SCENARIO)
        self.assertTrue(res["lifecycle"]["complete"])
        self.assertEqual(res["counts"]["events"]["total"], 4)
        self.assertEqual(res["counts"]["snapshot"]["total"], 4)
        self.assertIn("Сопоставление агрегатов", res["limitation"])
        self.assertEqual(res["differences"], [])

    def test_match_zero_runs(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "sample", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 0)
        self.assertEqual(res["status"], "match")
        self.assertTrue(res["match"])
        self.assertEqual(res["counts"]["events"]["total"], 0)
        self.assertEqual(res["counts"]["snapshot"]["total"], 0)

    def test_match_equal_monotonic_elapsed_sec(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 1.0},
            {"type": "sample", "elapsed_sec": 1.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 0)
        self.assertEqual(res["status"], "match")

    def test_cli_match_text_and_json(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 0.5},
            {"type": "shutdown", "elapsed_sec": 1.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        # In-process CLI run with --json
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            ret = main(["--events", str(ev_path), "--snapshot", str(db_path), "--json"])
        self.assertEqual(ret, 0)
        payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(payload["status"], "match")
        self.assertIn(LIMITATION_NOTE, payload["limitation"])

        # Text mode
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            ret = main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 0)
        output = stdout_buf.getvalue()
        self.assertIn("Status: MATCH", output)
        self.assertIn(LIMITATION_NOTE, output)


class TestNoSideEffectFiles(BaseAuditTest):
    """Verifies that audit creates no temporary or database side-effect files."""

    def test_no_side_effects_on_closed_snapshot(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        # Capture initial state of the directory
        initial_files = set(os.listdir(self.dir_path))

        exit_code, _ = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 0)

        # Verify no -wal, -shm, -journal, or temporary files created
        after_files = set(os.listdir(self.dir_path))
        self.assertEqual(initial_files, after_files)

    def test_read_only_filesystem_mode(self) -> None:
        """Snapshot file marked chmod 444 must be readable with mode=ro&immutable=1 without file errors."""
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "shutdown", "elapsed_sec": 1.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        os.chmod(db_path, 0o444)

        try:
            exit_code, res = audit(str(ev_path), str(db_path))
            self.assertEqual(exit_code, 0)
            self.assertEqual(res["status"], "match")
        finally:
            os.chmod(db_path, 0o644)


class TestExtraOrLostStatuses(BaseAuditTest):
    """Tests discrepancies in terminal status counts or scenarios (exit 1)."""

    def test_lost_status_in_snapshot(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 2.0},
            {"type": "shutdown", "elapsed_sec": 3.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "mismatch")
        self.assertFalse(res["match"])
        self.assertTrue(any("succeeded" in d for d in res["differences"]))

    def test_extra_status_in_snapshot(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "failed", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [
            ("r1", "usb_diagnostics", "failed"),
            ("r2", "usb_diagnostics", "failed"),
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "mismatch")

    def test_swapped_statuses_same_total(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "failed")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "mismatch")

    def test_extra_scenario_in_snapshot_blocks_confirmation(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [
            ("r1", "usb_diagnostics", "succeeded"),
            ("r2", "other_scenario", "succeeded"),
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "mismatch")
        self.assertTrue(any("usb_diagnostics" in d for d in res["differences"]))

    def test_unrecognized_status_in_snapshot(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [
            ("r1", "usb_diagnostics", "succeeded"),
            ("r2", "usb_diagnostics", "aborted_unknown"),
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "mismatch")


class TestUnconfirmedCompletion(BaseAuditTest):
    """Tests missing lifecycle boundaries (start/shutdown) or pending runs (exit 1)."""

    def test_missing_start_event(self) -> None:
        events = [
            {"type": "sample", "elapsed_sec": 0.5},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")
        self.assertFalse(res["lifecycle"]["has_start"])
        self.assertFalse(res["lifecycle"]["complete"])
        self.assertTrue(any("start" in d for d in res["differences"]))

    def test_missing_shutdown_event(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")
        self.assertFalse(res["lifecycle"]["has_shutdown"])
        self.assertFalse(res["lifecycle"]["complete"])
        self.assertTrue(any("shutdown" in d for d in res["differences"]))

    def test_neither_start_nor_shutdown(self) -> None:
        events = [
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")

    def test_shutdown_before_start(self) -> None:
        events = [
            {"type": "shutdown", "elapsed_sec": 0.5},
            {"type": "start", "elapsed_sec": 1.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")
        self.assertTrue(any("before" in d for d in res["differences"]))

    def test_events_after_shutdown(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "shutdown", "elapsed_sec": 1.0},
            {"type": "sample", "elapsed_sec": 2.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")
        self.assertTrue(any("after" in d for d in res["differences"]))

    def test_snapshot_queued_runs_blocks_confirmation(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [
            ("r1", "usb_diagnostics", "succeeded"),
            ("r2", "usb_diagnostics", "queued"),
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")
        self.assertTrue(any("queued" in d for d in res["differences"]))

    def test_snapshot_running_runs_blocks_confirmation(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [
            ("r1", "usb_diagnostics", "succeeded"),
            ("r2", "usb_diagnostics", "running"),
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        exit_code, res = audit(str(ev_path), str(db_path))
        self.assertEqual(exit_code, 1)
        self.assertEqual(res["status"], "incomplete")
        self.assertTrue(any("running" in d for d in res["differences"]))


class TestEmptyAndCorruptedInputs(BaseAuditTest):
    """Tests bad input validation, corruption rejection, and exit code 2."""

    def run_main(self, args: list[str]) -> int:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return main(args)

    def test_missing_events_file(self) -> None:
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(self.dir_path / "absent.jsonl"), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_missing_snapshot_file(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(self.dir_path / "absent.db")])
        self.assertEqual(ret, 2)

    def test_empty_events_file(self) -> None:
        ev_path = self.dir_path / "empty.jsonl"
        ev_path.touch()
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_file_only_whitespace(self) -> None:
        ev_path = self.dir_path / "blank.jsonl"
        ev_path.write_text("   \n\n\t  \n", encoding="utf-8")
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_empty_snapshot_file(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}, {"type": "shutdown", "elapsed_sec": 1.0}])
        db_path = self.dir_path / "empty.db"
        db_path.touch()
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_corrupted_snapshot_file(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}, {"type": "shutdown", "elapsed_sec": 1.0}])
        db_path = self.dir_path / "corrupt.db"
        db_path.write_bytes(b"NOT A SQLITE DATABASE GARBAGE DATA 12345")
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_snapshot_missing_runs_table(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}, {"type": "shutdown", "elapsed_sec": 1.0}])
        db_path = self.create_snapshot([], schema="CREATE TABLE other_table (id INT);")
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_snapshot_missing_required_columns(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}, {"type": "shutdown", "elapsed_sec": 1.0}])
        db_path = self.create_snapshot([], schema="CREATE TABLE runs (run_id TEXT, status TEXT);")
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_snapshot_non_empty_wal_rejected(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}, {"type": "shutdown", "elapsed_sec": 1.0}])
        db_path = self.create_snapshot([])
        wal_path = Path(str(db_path) + "-wal")
        wal_path.write_bytes(b"wal-data-active-pilot")

        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_snapshot_empty_wal_allowed(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "shutdown", "elapsed_sec": 1.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        wal_path = Path(str(db_path) + "-wal")
        wal_path.touch()  # 0 bytes

        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 0)

    def test_snapshot_non_empty_journal_rejected(self) -> None:
        ev_path = self.create_events([{"type": "start", "elapsed_sec": 0.0}, {"type": "shutdown", "elapsed_sec": 1.0}])
        db_path = self.create_snapshot([])
        journal_path = Path(str(db_path) + "-journal")
        journal_path.write_bytes(b"journal-rollback-data")

        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_snapshot_empty_journal_allowed(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "shutdown", "elapsed_sec": 1.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        journal_path = Path(str(db_path) + "-journal")
        journal_path.touch()  # 0 bytes

        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 0)

    def test_events_invalid_json_syntax(self) -> None:
        ev_path = self.dir_path / "bad.jsonl"
        ev_path.write_text('{"type": "start", "elapsed_sec": 0.0}\n{not-json}\n', encoding="utf-8")
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_json_not_object(self) -> None:
        ev_path = self.dir_path / "array.jsonl"
        ev_path.write_text('["start", 0.0]\n', encoding="utf-8")
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_contains_run_id(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0, "run_id": "r_forbidden"},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_invalid_type(self) -> None:
        events = [
            {"type": "unknown_type", "elapsed_sec": 0.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_missing_elapsed_sec(self) -> None:
        events = [
            {"type": "start"},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_invalid_elapsed_sec_type(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": "not_a_number"},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_negative_elapsed_sec(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": -1.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_non_monotonic_elapsed_sec(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 2.0},
            {"type": "sample", "elapsed_sec": 1.5},
            {"type": "shutdown", "elapsed_sec": 3.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_run_missing_status(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_events_run_invalid_status(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "running", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot([])
        ret = self.run_main(["--events", str(ev_path), "--snapshot", str(db_path)])
        self.assertEqual(ret, 2)

    def test_cli_missing_args(self) -> None:
        ret = self.run_main([])
        self.assertEqual(ret, 2)

    def test_cli_unknown_args(self) -> None:
        ret = self.run_main(["--events", "foo", "--snapshot", "bar", "--bogus"])
        self.assertEqual(ret, 2)


class TestPrivacyAndSubprocessCLI(BaseAuditTest):
    """Verifies absence of raw strings/PII in outputs and tests CLI subprocess invocation."""

    def test_no_raw_strings_in_error_or_json(self) -> None:
        ev_path = self.dir_path / "secret_events.jsonl"
        secret_line = '{"type": "run", "status": "succeeded", "elapsed_sec": 1.0, "secret_token": "SUPER_SECRET_PII_12345"}'
        # Followed by invalid non-monotonic line
        ev_path.write_text(f'{secret_line}\n{{"type": "start", "elapsed_sec": 0.5}}\n', encoding="utf-8")
        db_path = self.create_snapshot([])

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            ret = main(["--events", str(ev_path), "--snapshot", str(db_path), "--json"])

        self.assertEqual(ret, 2)
        combined = stdout_buf.getvalue() + stderr_buf.getvalue()
        self.assertNotIn("SUPER_SECRET_PII_12345", combined)
        self.assertNotIn("secret_token", combined)

    def test_subprocess_cli_invocation(self) -> None:
        events = [
            {"type": "start", "elapsed_sec": 0.0},
            {"type": "run", "status": "succeeded", "elapsed_sec": 1.0},
            {"type": "shutdown", "elapsed_sec": 2.0},
        ]
        db_rows = [("r1", "usb_diagnostics", "succeeded")]
        ev_path = self.create_events(events)
        db_path = self.create_snapshot(db_rows)

        cmd = [
            sys.executable,
            "scripts/audit_desktop_pilot_journal.py",
            "--events",
            str(ev_path),
            "--snapshot",
            str(db_path),
            "--json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "match")
        self.assertTrue(parsed["match"])


if __name__ == "__main__":
    unittest.main()
