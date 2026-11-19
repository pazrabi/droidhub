#!/usr/bin/env python3
"""Tests for scripts/record_desktop_usb_hotplug.py."""

from __future__ import annotations

import io
import itertools
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

# Ensure scripts package / module is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import record_desktop_usb_hotplug as rdu


class TestRecordDesktopUsbHotplug(unittest.TestCase):
    """Test suite for record_desktop_usb_hotplug utility."""

    def test_help_text_requirements(self) -> None:
        """Help text must describe operator reaction and provide brief example."""
        parser = rdu.get_parser()
        help_text = parser.format_help()

        # Contract: mentions operator reaction time and diagnostic estimate
        self.assertIn(
            "задержка включает реакцию операторов, отметки дают диагностическую оценку",
            help_text,
        )

        # Contract: brief example included
        self.assertIn("--output", help_text)
        self.assertIn("disconnect marker", help_text)
        self.assertIn("quit", help_text)

    def test_time_injection_and_exact_jsonl(self) -> None:
        """Test with injected clock and verify exact JSONL formatting and file mode."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "events.jsonl")

            # 1 start_time + 3 event lines + 1 quit line = 5 time reads
            times = [100.0, 100.5, 102.25, 103.0, 105.0]
            time_iter = iter(times)

            def fake_time() -> float:
                return next(time_iter)

            input_data = io.StringIO(
                "1 disconnect marker\n"
                "1 disconnect adb\n"
                "1 disconnect park\n"
                "quit\n"
            )

            events = rdu.record_events(
                out_path,
                input_stream=input_data,
                time_func=fake_time,
            )

            # Check returned data structure
            expected = [
                {"cycle": 1, "edge": "disconnect", "kind": "marker", "elapsed_sec": 0.5},
                {"cycle": 1, "edge": "disconnect", "kind": "adb", "elapsed_sec": 2.25},
                {"cycle": 1, "edge": "disconnect", "kind": "park", "elapsed_sec": 3.0},
            ]
            self.assertEqual(events, expected)

            # Check file permissions (0600)
            mode = stat.S_IMODE(os.stat(out_path).st_mode)
            self.assertEqual(mode, 0o600)

            # Verify exact JSONL file contents
            with open(out_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            self.assertEqual(len(lines), 3)
            parsed_lines = [json.loads(line) for line in lines]
            self.assertEqual(parsed_lines, expected)

            for line_data in parsed_lines:
                self.assertIsInstance(line_data["cycle"], int)
                self.assertGreater(line_data["cycle"], 0)
                self.assertIn(line_data["edge"], ("disconnect", "reconnect"))
                self.assertIn(line_data["kind"], ("marker", "adb", "park"))
                self.assertIsInstance(line_data["elapsed_sec"], (int, float))
                self.assertGreaterEqual(line_data["elapsed_sec"], 0.0)

    def test_reconnect_and_asynchronous_order_and_skips(self) -> None:
        """adb and park can arrive in any order, reconnect works, skips allowed."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "async.jsonl")
            clock_it = itertools.count(0.0, 1.0)

            input_data = io.StringIO(
                "2 reconnect park\n"
                "2 reconnect adb\n"
                "2 reconnect marker\n"
                "5 disconnect marker\n"
                "quit\n"
            )

            events = rdu.record_events(
                out_path,
                input_stream=input_data,
                time_func=lambda: next(clock_it),
            )

            self.assertEqual(len(events), 4)
            self.assertEqual(events[0]["kind"], "park")
            self.assertEqual(events[1]["kind"], "adb")
            self.assertEqual(events[2]["kind"], "marker")
            self.assertEqual(events[3]["cycle"], 5)  # cycle skip allowed

    def test_duplicate_rejection_preserves_previous(self) -> None:
        """Duplicate (cycle, edge, kind) must be rejected, preserving previous."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "duplicates.jsonl")
            err_buf = io.StringIO()
            clock_it = itertools.count(0.0, 1.0)

            input_data = io.StringIO(
                "1 disconnect marker\n"
                "1 disconnect marker\n"  # duplicate
                "1 disconnect adb\n"
                "1 disconnect adb\n"  # duplicate
                "1 reconnect marker\n"  # different edge -> valid
                "1 disconnect marker\n"  # duplicate again
                "quit\n"
            )

            events = rdu.record_events(
                out_path,
                input_stream=input_data,
                time_func=lambda: next(clock_it),
                err_stream=err_buf,
            )

            # Only 3 events should be recorded
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0], {"cycle": 1, "edge": "disconnect", "kind": "marker", "elapsed_sec": 1.0})
            self.assertEqual(events[1], {"cycle": 1, "edge": "disconnect", "kind": "adb", "elapsed_sec": 3.0})
            self.assertEqual(events[2], {"cycle": 1, "edge": "reconnect", "kind": "marker", "elapsed_sec": 5.0})

            # Rejections should be logged to err_stream
            err_output = err_buf.getvalue()
            self.assertIn("Rejected duplicate event", err_output)
            self.assertEqual(err_output.count("Rejected duplicate event"), 3)

            # Check file contains only the 3 valid events
            with open(out_path, "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f]
            self.assertEqual(lines, events)

    def test_invalid_lines_rejected(self) -> None:
        """Invalid lines are ignored, keeping only valid events."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "invalid.jsonl")
            err_buf = io.StringIO()
            clock_it = itertools.count(0.0, 1.0)

            input_data = io.StringIO(
                "\n"
                "   \n"
                "badline\n"
                "1 disconnect\n"
                "1 disconnect marker extra\n"
                "0 disconnect marker\n"  # cycle must be positive
                "-1 disconnect marker\n"
                "abc disconnect marker\n"
                "1.5 disconnect marker\n"
                "1 badedge marker\n"
                "1 disconnect badkind\n"
                "1 disconnect marker\n"  # valid!
                "quit\n"
            )

            events = rdu.record_events(
                out_path,
                input_stream=input_data,
                time_func=lambda: next(clock_it),
                err_stream=err_buf,
            )

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["cycle"], 1)
            self.assertEqual(events[0]["edge"], "disconnect")
            self.assertEqual(events[0]["kind"], "marker")

            with open(out_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)

    def test_cancellation_keyboard_interrupt_leaves_valid_partial_file(self) -> None:
        """KeyboardInterrupt (Ctrl+C) leaves a usable partial file without corruption."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "interrupted.jsonl")

            def gen_with_interrupt():
                yield "1 disconnect marker\n"
                raise KeyboardInterrupt()

            events = rdu.record_events(
                out_path,
                input_stream=gen_with_interrupt(),
                time_func=lambda: 5.0,
            )

            self.assertEqual(len(events), 1)
            self.assertTrue(os.path.exists(out_path))

            with open(out_path, "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["kind"], "marker")

    def test_eof_leaves_valid_partial_file(self) -> None:
        """Stream EOF leaves usable partial file without needing 'quit'."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "eof.jsonl")
            input_data = io.StringIO("1 disconnect marker\n1 disconnect adb\n")

            events = rdu.record_events(out_path, input_stream=input_data)
            self.assertEqual(len(events), 2)

            with open(out_path, "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f]
            self.assertEqual(len(lines), 2)

    def test_file_conflict_already_exists(self) -> None:
        """O_EXCL ensures pre-existing files cause an error and are not overwritten."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "existing.jsonl")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("existing content\n")

            with self.assertRaises(FileExistsError):
                rdu.create_output_file(out_path)

            # CLI main should return non-zero
            ret = rdu.main(["--output", out_path])
            self.assertNotEqual(ret, 0)

            # Content was untouched
            with open(out_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "existing content\n")

    def test_broken_symlink_not_followed(self) -> None:
        """Even a broken symlink must not be followed or bypassed."""
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "nonexistent_target.jsonl")
            symlink = os.path.join(td, "broken_link.jsonl")
            os.symlink(target, symlink)

            self.assertFalse(os.path.exists(target))

            with self.assertRaises(FileExistsError):
                rdu.create_output_file(symlink)

            ret = rdu.main(["--output", symlink])
            self.assertNotEqual(ret, 0)

            # Target must NOT have been created
            self.assertFalse(os.path.exists(target))

    def test_symlink_to_existing_file_rejected(self) -> None:
        """Symlink pointing to existing file is also rejected."""
        with tempfile.TemporaryDirectory() as td:
            real_file = os.path.join(td, "real.jsonl")
            with open(real_file, "w") as f:
                f.write("original")
            symlink = os.path.join(td, "link.jsonl")
            os.symlink(real_file, symlink)

            ret = rdu.main(["--output", symlink])
            self.assertNotEqual(ret, 0)
            with open(real_file, "r") as f:
                self.assertEqual(f.read(), "original")

    def test_io_error_nonexistent_directory(self) -> None:
        """IO error when directory doesn't exist exits with non-zero code."""
        bad_path = "/nonexistent_dir_hotplug_test/out.jsonl"
        ret = rdu.main(["--output", bad_path])
        self.assertNotEqual(ret, 0)

    def test_cli_missing_output_argument(self) -> None:
        """CLI without required --output returns non-zero code."""
        ret = rdu.main([])
        self.assertNotEqual(ret, 0)

    def test_cli_full_run_with_stdin(self) -> None:
        """Full CLI invocation via main() reading from mock sys.stdin."""
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "cli_out.jsonl")
            mock_stdin = io.StringIO(
                "1 disconnect marker\n"
                "1 disconnect adb\n"
                "quit\n"
            )
            old_stdin = sys.stdin
            try:
                sys.stdin = mock_stdin
                code = rdu.main(["--output", out_path])
                self.assertEqual(code, 0)
            finally:
                sys.stdin = old_stdin

            with open(out_path, "r", encoding="utf-8") as f:
                lines = [json.loads(l) for l in f]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["kind"], "marker")
            self.assertEqual(lines[1]["kind"], "adb")


if __name__ == "__main__":
    unittest.main()
