"""
tests/test_desktop_usb_pilot_preflight.py - Comprehensive tests for desktop_usb_pilot_preflight.py.

Tests cover:
- macOS arm64 platform checks (Darwin arm64/aarch64 vs other).
- App bundle binary verification and symlink containment (Resources vs Frameworks adb, executable bits).
- Paths with spaces for app bundle, workspace, and process comm.
- Exact Desktop PID verification via ps -p PID -o pid=,comm= (mismatch -> fail; error/unreadable -> incomplete).
- Workspace check (exists, is_dir, writable, free space reported without threshold).
- ADB check: bundled adb devices -l (no shell, small timeout).
  Both target phones, state=device, usb: token, positive transport_id.
- Filtering out network and emulator devices.
  Ambiguous transport (duplicate transport_id, missing/non-positive transport_id, duplicate serial).
- Daemon restart detection (daemon messages in stdout/stderr -> incomplete).
- ADB timeout and failure (incomplete).
- Privacy: no serials, no PII paths, no raw stdout/stderr in report.
- CLI argument validation and --help daemon notice.
- JSON and text output formatting and exit codes.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.desktop_usb_pilot_preflight import (
    AdbCheckResult,
    build_parser,
    check_adb_devices,
    check_app_bundle,
    check_desktop_pid,
    check_platform,
    check_workspace,
    format_text_report,
    main,
    parse_adb_devices_output,
    run_preflight,
)


class TestPlatformCheck(unittest.TestCase):
    def test_darwin_arm64(self) -> None:
        self.assertEqual(check_platform("Darwin", "arm64"), "ok")
        self.assertEqual(check_platform("darwin", "aarch64"), "ok")

    def test_non_arm64_or_non_darwin(self) -> None:
        self.assertEqual(check_platform("Darwin", "x86_64"), "fail")
        self.assertEqual(check_platform("Linux", "x86_64"), "fail")
        self.assertEqual(check_platform("Linux", "arm64"), "fail")
        self.assertEqual(check_platform("Windows", "AMD64"), "fail")


class TestAppBundleCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_mock_bundle(
        self,
        app_name: str = "DroidHub.app",
        adb_subpath: str = "Contents/Resources/tools/adb",
    ) -> Path:
        app_dir = Path(self.test_dir) / app_name
        droidhub_bin = app_dir / "Contents" / "MacOS" / "DroidHub"
        adb_bin = app_dir / adb_subpath

        droidhub_bin.parent.mkdir(parents=True, exist_ok=True)
        adb_bin.parent.mkdir(parents=True, exist_ok=True)

        droidhub_bin.write_text("#!/bin/sh\nexit 0\n")
        droidhub_bin.chmod(0o755)

        adb_bin.write_text("#!/bin/sh\nexit 0\n")
        adb_bin.chmod(0o755)

        return app_dir

    def test_valid_bundle_with_resources_adb(self) -> None:
        app_dir = self._create_mock_bundle("DroidHub.app", "Contents/Resources/tools/adb")
        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "ok")
        self.assertIsNotNone(dh)
        self.assertIsNotNone(adb)
        self.assertTrue(dh.is_file())
        self.assertTrue(adb.is_file())

    def test_valid_bundle_with_frameworks_adb(self) -> None:
        app_dir = self._create_mock_bundle("DroidHub.app", "Contents/Frameworks/tools/adb")
        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "ok")
        self.assertIsNotNone(dh)
        self.assertIsNotNone(adb)

    def test_missing_app_dir(self) -> None:
        non_existent = Path(self.test_dir) / "Missing.app"
        status, dh, adb = check_app_bundle(non_existent)
        self.assertEqual(status, "fail")
        self.assertIsNone(dh)
        self.assertIsNone(adb)

    def test_missing_droidhub_binary(self) -> None:
        app_dir = self._create_mock_bundle()
        (app_dir / "Contents" / "MacOS" / "DroidHub").unlink()
        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "fail")

    def test_missing_adb_binary(self) -> None:
        app_dir = self._create_mock_bundle()
        (app_dir / "Contents" / "Resources" / "tools" / "adb").unlink()
        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "fail")

    def test_droidhub_not_executable(self) -> None:
        app_dir = self._create_mock_bundle()
        (app_dir / "Contents" / "MacOS" / "DroidHub").chmod(0o644)
        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "fail")

    def test_adb_not_executable(self) -> None:
        app_dir = self._create_mock_bundle()
        (app_dir / "Contents" / "Resources" / "tools" / "adb").chmod(0o644)
        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "fail")

    def test_symlink_inside_bundle(self) -> None:
        app_dir = Path(self.test_dir) / "SymlinkApp.app"
        real_bin = app_dir / "Contents" / "SharedSupport" / "DroidHub_real"
        real_bin.parent.mkdir(parents=True, exist_ok=True)
        real_bin.write_text("#!/bin/sh\nexit 0\n")
        real_bin.chmod(0o755)

        link_bin = app_dir / "Contents" / "MacOS" / "DroidHub"
        link_bin.parent.mkdir(parents=True, exist_ok=True)
        link_bin.symlink_to(real_bin)

        adb_bin = app_dir / "Contents" / "Resources" / "tools" / "adb"
        adb_bin.parent.mkdir(parents=True, exist_ok=True)
        adb_bin.write_text("#!/bin/sh\nexit 0\n")
        adb_bin.chmod(0o755)

        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "ok")
        self.assertEqual(dh, real_bin.resolve())

    def test_symlink_escaping_bundle(self) -> None:
        app_dir = Path(self.test_dir) / "EscapeApp.app"
        outside_bin = Path(self.test_dir) / "outside_droidhub"
        outside_bin.write_text("#!/bin/sh\nexit 0\n")
        outside_bin.chmod(0o755)

        link_bin = app_dir / "Contents" / "MacOS" / "DroidHub"
        link_bin.parent.mkdir(parents=True, exist_ok=True)
        link_bin.symlink_to(outside_bin)

        adb_bin = app_dir / "Contents" / "Resources" / "tools" / "adb"
        adb_bin.parent.mkdir(parents=True, exist_ok=True)
        adb_bin.write_text("#!/bin/sh\nexit 0\n")
        adb_bin.chmod(0o755)

        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "fail")

    def test_intermediate_dir_symlink_escaping_bundle(self) -> None:
        app_dir = Path(self.test_dir) / "EscapeDirApp.app"
        app_dir.mkdir(parents=True, exist_ok=True)
        contents = app_dir / "Contents"
        contents.mkdir(parents=True, exist_ok=True)

        outside_macos = Path(self.test_dir) / "outside_macos"
        outside_macos.mkdir(parents=True, exist_ok=True)
        outside_dh = outside_macos / "DroidHub"
        outside_dh.write_text("#!/bin/sh\nexit 0\n")
        outside_dh.chmod(0o755)

        # Contents/MacOS is a symlink pointing outside the bundle
        (contents / "MacOS").symlink_to(outside_macos)

        adb_bin = contents / "Resources" / "tools" / "adb"
        adb_bin.parent.mkdir(parents=True, exist_ok=True)
        adb_bin.write_text("#!/bin/sh\nexit 0\n")
        adb_bin.chmod(0o755)

        status, dh, adb = check_app_bundle(app_dir)
        self.assertEqual(status, "fail")


class TestPathsWithSpaces(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="pilot test spaces ")

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_paths_with_spaces_bundle_and_comm(self) -> None:
        app_dir = Path(self.test_dir) / "My Droid Hub.app"
        dh = app_dir / "Contents" / "MacOS" / "DroidHub"
        adb = app_dir / "Contents" / "Resources" / "tools" / "adb"
        dh.parent.mkdir(parents=True, exist_ok=True)
        adb.parent.mkdir(parents=True, exist_ok=True)
        dh.write_text("#!/bin/sh\nexit 0\n")
        dh.chmod(0o755)
        adb.write_text("#!/bin/sh\nexit 0\n")
        adb.chmod(0o755)

        status, dh_resolved, adb_resolved = check_app_bundle(app_dir)
        self.assertEqual(status, "ok")
        self.assertIn(" ", str(dh_resolved))

        # Check PID verification with space in comm path
        pid = 9876
        ps_output = f"  {pid} {dh_resolved}\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ps", "-p", str(pid), "-o", "pid=,comm="],
                returncode=0,
                stdout=ps_output,
                stderr="",
            )
            pid_status = check_desktop_pid(pid, dh_resolved)
            self.assertEqual(pid_status, "ok")

        # Check workspace with spaces
        ws_dir = Path(self.test_dir) / "My Workspace Dir"
        ws_dir.mkdir(parents=True, exist_ok=True)
        ws_status, free_bytes = check_workspace(ws_dir)
        self.assertEqual(ws_status, "ok")
        self.assertGreater(free_bytes, 0)


class TestDesktopPidCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.expected_bin = Path(self.test_dir) / "Contents" / "MacOS" / "DroidHub"
        self.expected_bin.parent.mkdir(parents=True, exist_ok=True)
        self.expected_bin.write_text("#!/bin/sh\n")
        self.expected_bin.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pid_exact_match(self) -> None:
        pid = 4321
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"{pid} {self.expected_bin}\n",
                stderr="",
            )
            self.assertEqual(check_desktop_pid(pid, self.expected_bin), "ok")

    def test_pid_mismatch_binary(self) -> None:
        pid = 4321
        other_bin = Path(self.test_dir) / "OtherApp"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"{pid} {other_bin}\n",
                stderr="",
            )
            self.assertEqual(check_desktop_pid(pid, self.expected_bin), "fail")

    def test_ps_returncode_error(self) -> None:
        pid = 4321
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="process not found",
            )
            self.assertEqual(check_desktop_pid(pid, self.expected_bin), "incomplete")

    def test_ps_unreadable_output(self) -> None:
        pid = 4321
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="garbled_output_without_comm",
                stderr="",
            )
            self.assertEqual(check_desktop_pid(pid, self.expected_bin), "incomplete")

    def test_ps_timeout(self) -> None:
        pid = 4321
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=5.0)):
            self.assertEqual(check_desktop_pid(pid, self.expected_bin), "incomplete")

    def test_ps_subprocess_error(self) -> None:
        pid = 4321
        with patch("subprocess.run", side_effect=OSError("Command not found")):
            self.assertEqual(check_desktop_pid(pid, self.expected_bin), "incomplete")

    def test_comm_resolve_oserror(self) -> None:
        pid = 4321
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"{pid} /some/comm/path\n",
                stderr="",
            )
            with patch.object(Path, "resolve", side_effect=PermissionError("Permission denied")):
                self.assertEqual(check_desktop_pid(pid, self.expected_bin), "incomplete")

    def test_comm_resolve_valueerror(self) -> None:
        pid = 4321
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"{pid} /some/comm/path\n",
                stderr="",
            )
            with patch.object(Path, "resolve", side_effect=ValueError("Invalid path")):
                self.assertEqual(check_desktop_pid(pid, self.expected_bin), "incomplete")


class TestWorkspaceCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_workspace_valid(self) -> None:
        ws = Path(self.test_dir) / "workspace"
        ws.mkdir()
        status, free_bytes = check_workspace(ws)
        self.assertEqual(status, "ok")
        self.assertGreater(free_bytes, 0)

    def test_workspace_missing(self) -> None:
        ws = Path(self.test_dir) / "missing_dir"
        status, free_bytes = check_workspace(ws)
        self.assertEqual(status, "fail")
        self.assertEqual(free_bytes, 0)

    def test_workspace_is_file(self) -> None:
        ws = Path(self.test_dir) / "file.txt"
        ws.write_text("not a dir")
        status, free_bytes = check_workspace(ws)
        self.assertEqual(status, "fail")
        self.assertEqual(free_bytes, 0)

    def test_workspace_read_only(self) -> None:
        ws = Path(self.test_dir) / "ro_dir"
        ws.mkdir()
        ws.chmod(0o444)
        try:
            status, _ = check_workspace(ws)
            # On root or in sandbox write access check may vary, verify os.access logic
            if not os.access(ws, os.W_OK):
                self.assertEqual(status, "fail")
        finally:
            ws.chmod(0o755)


class TestAdbDevicesCheck(unittest.TestCase):
    def test_both_phones_ok(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 product:walleye model:Pixel_2 transport_id:1\n"
            "SERIAL_B               device usb:1-2 product:walleye model:Pixel_2 transport_id:2\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.targets_matched, 2)
        self.assertEqual(res.usb_detected, 2)
        self.assertEqual(res.network_filtered, 0)
        self.assertEqual(res.emulator_filtered, 0)
        self.assertEqual(res.duplicate_filtered, 0)

    def test_target_missing(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 1)

    def test_target_unauthorized_and_offline(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               unauthorized usb:1-2 transport_id:2\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 1)

        stdout_offline = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               offline usb:1-2 transport_id:2\n"
        )
        res_offline = parse_adb_devices_output(stdout_offline, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res_offline.status, "fail")
        self.assertEqual(res_offline.targets_matched, 1)

    def test_target_without_usb_token(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               device product:raven model:Pixel_6 transport_id:2\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 1)

    def test_network_and_emulator_devices_filtered(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               device usb:1-2 transport_id:2\n"
            "192.168.1.100:5555     device product:raven transport_id:3\n"
            "emulator-5554          device product:sdk transport_id:4\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.targets_matched, 2)
        self.assertEqual(res.network_filtered, 1)
        self.assertEqual(res.emulator_filtered, 1)

    def test_target_is_network_or_emulator(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "emulator-5554          device usb:1-2 transport_id:2\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "emulator-5554"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 1)
        self.assertEqual(res.emulator_filtered, 1)

    def test_ambiguous_transport_duplicate_transport_id(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:5\n"
            "SERIAL_B               device usb:1-2 transport_id:5\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 0)
        self.assertGreater(res.duplicate_filtered, 0)

    def test_invalid_transport_id_zero_or_negative(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               device usb:1-2 transport_id:0\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 1)

    def test_duplicate_serial_in_adb_output(self) -> None:
        stdout = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_A               device usb:1-2 transport_id:2\n"
            "SERIAL_B               device usb:1-3 transport_id:3\n"
        )
        res = parse_adb_devices_output(stdout, ["SERIAL_A", "SERIAL_B"])
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.targets_matched, 1)
        self.assertGreater(res.duplicate_filtered, 0)

    def test_duplicate_target_serials_in_targets_input(self) -> None:
        # Case 1: single device attached, duplicate target requested -> must fail, matched=1
        stdout_one = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
        )
        res_one = parse_adb_devices_output(stdout_one, ["SERIAL_A", "SERIAL_A"])
        self.assertEqual(res_one.status, "fail")
        self.assertEqual(res_one.targets_matched, 1)

        # Case 2: two distinct devices attached, duplicate target requested -> must fail
        stdout_two = (
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               device usb:1-2 transport_id:2\n"
        )
        res_two = parse_adb_devices_output(stdout_two, ["SERIAL_A", "SERIAL_A"])
        self.assertEqual(res_two.status, "fail")
        self.assertEqual(res_two.targets_matched, 1)

    def test_daemon_restart_detected(self) -> None:
        adb_path = Path("/mock/adb")
        restart_stdout = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "List of devices attached\n"
            "SERIAL_A               device usb:1-1 transport_id:1\n"
            "SERIAL_B               device usb:1-2 transport_id:2\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=restart_stdout,
                stderr="",
            )
            res = check_adb_devices(adb_path, ["SERIAL_A", "SERIAL_B"])
            self.assertEqual(res.status, "incomplete")

    def test_daemon_killing_detected(self) -> None:
        adb_path = Path("/mock/adb")
        restart_stdout = (
            "adb server version (41) doesn't match this client (39); killing...\n"
            "* daemon not running; starting now at tcp:5037\n"
            "List of devices attached\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=restart_stdout,
                stderr="",
            )
            res = check_adb_devices(adb_path, ["SERIAL_A", "SERIAL_B"])
            self.assertEqual(res.status, "incomplete")

    def test_adb_timeout(self) -> None:
        adb_path = Path("/mock/adb")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=5.0)):
            res = check_adb_devices(adb_path, ["SERIAL_A", "SERIAL_B"])
            self.assertEqual(res.status, "incomplete")

    def test_adb_crash_non_zero(self) -> None:
        adb_path = Path("/mock/adb")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=255,
                stdout="",
                stderr="segmentation fault",
            )
            res = check_adb_devices(adb_path, ["SERIAL_A", "SERIAL_B"])
            self.assertEqual(res.status, "incomplete")


class TestPrivacyAndSanitization(unittest.TestCase):
    def test_report_contains_no_serials_paths_or_raw_output(self) -> None:
        report = {
            "status": "ok",
            "overall_status": "ok",
            "checks": {
                "platform": "ok",
                "app": "ok",
                "desktop_pid": "ok",
                "workspace": "ok",
                "devices": "ok",
            },
            "counters": {
                "targets_expected": 2,
                "targets_matched": 2,
                "usb_devices_detected": 2,
                "network_devices_filtered": 0,
                "emulator_devices_filtered": 0,
                "duplicate_devices_filtered": 0,
                "workspace_free_bytes": 10000000,
            },
            "workspace_free_bytes": 10000000,
            "targets_expected": 2,
            "targets_matched": 2,
        }

        # Format text report
        text_out = format_text_report(report)
        json_out = json.dumps(report)

        secret_serial1 = "SERIAL_X_9999"
        secret_serial2 = "SERIAL_Y_8888"
        secret_pii_path = "/Users/secret_pii_user/stand"
        raw_adb_line = "List of devices attached"

        for output in (text_out, json_out):
            self.assertNotIn(secret_serial1, output)
            self.assertNotIn(secret_serial2, output)
            self.assertNotIn(secret_pii_path, output)
            self.assertNotIn(raw_adb_line, output)
            self.assertIn("overall_status" if output is json_out else "Overall status:", output)
            self.assertIn("targets_matched", output)


class TestCliAndHelp(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.app_dir = Path(self.test_dir) / "DroidHub.app"
        dh = self.app_dir / "Contents" / "MacOS" / "DroidHub"
        adb = self.app_dir / "Contents" / "Resources" / "tools" / "adb"
        dh.parent.mkdir(parents=True, exist_ok=True)
        adb.parent.mkdir(parents=True, exist_ok=True)
        dh.write_text("#!/bin/sh\nexit 0\n")
        dh.chmod(0o755)
        adb.write_text("#!/bin/sh\nexit 0\n")
        adb.chmod(0o755)

        self.ws_dir = Path(self.test_dir) / "workspace"
        self.ws_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_help_contains_daemon_notice(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        self.assertIn("adb devices", help_text)
        self.assertIn("local ADB server daemon", help_text)

    def test_cli_requires_positive_pid(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as cm:
                main([
                    "--app", str(self.app_dir),
                    "--workspace", str(self.ws_dir),
                    "--desktop-pid", "0",
                    "--device", "DEV1",
                    "--device", "DEV2",
                ])
            self.assertEqual(cm.exception.code, 2)

    def test_cli_requires_exactly_two_distinct_devices(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            # 1 device
            with self.assertRaises(SystemExit) as cm1:
                main([
                    "--app", str(self.app_dir),
                    "--workspace", str(self.ws_dir),
                    "--desktop-pid", "123",
                    "--device", "DEV1",
                ])
            self.assertEqual(cm1.exception.code, 2)

            # duplicate devices
            with self.assertRaises(SystemExit) as cm2:
                main([
                    "--app", str(self.app_dir),
                    "--workspace", str(self.ws_dir),
                    "--desktop-pid", "123",
                    "--device", "DEV1",
                    "--device", "DEV1",
                ])
            self.assertEqual(cm2.exception.code, 2)

    def test_main_success_flow(self) -> None:
        pid = 12345
        dh_bin = (self.app_dir / "Contents" / "MacOS" / "DroidHub").resolve()

        adb_stdout = (
            "List of devices attached\n"
            "DEV1                   device usb:1-1 transport_id:1\n"
            "DEV2                   device usb:1-2 transport_id:2\n"
        )

        def mock_subprocess_run(cmd, *args, **kwargs):
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"  {pid} {dh_bin}\n",
                    stderr="",
                )
            elif "devices" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=adb_stdout,
                    stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_subprocess_run), \
             patch("platform.system", return_value="Darwin"), \
             patch("platform.machine", return_value="arm64"), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:

            exit_code = main([
                "--app", str(self.app_dir),
                "--workspace", str(self.ws_dir),
                "--desktop-pid", str(pid),
                "--device", "DEV1",
                "--device", "DEV2",
                "--json",
            ])

            self.assertEqual(exit_code, 0)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["checks"]["platform"], "ok")
            self.assertEqual(data["checks"]["app"], "ok")
            self.assertEqual(data["checks"]["desktop_pid"], "ok")
            self.assertEqual(data["checks"]["workspace"], "ok")
            self.assertEqual(data["checks"]["devices"], "ok")
            self.assertEqual(data["counters"]["targets_matched"], 2)

    def test_main_daemon_restart_gives_incomplete_and_exit_1(self) -> None:
        pid = 12345
        dh_bin = (self.app_dir / "Contents" / "MacOS" / "DroidHub").resolve()

        adb_stdout = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "List of devices attached\n"
            "DEV1                   device usb:1-1 transport_id:1\n"
            "DEV2                   device usb:1-2 transport_id:2\n"
        )

        def mock_subprocess_run(cmd, *args, **kwargs):
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"  {pid} {dh_bin}\n",
                    stderr="",
                )
            elif "devices" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=adb_stdout,
                    stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_subprocess_run), \
             patch("platform.system", return_value="Darwin"), \
             patch("platform.machine", return_value="arm64"), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:

            exit_code = main([
                "--app", str(self.app_dir),
                "--workspace", str(self.ws_dir),
                "--desktop-pid", str(pid),
                "--device", "DEV1",
                "--device", "DEV2",
                "--json",
            ])

            self.assertEqual(exit_code, 1)
            data = json.loads(mock_stdout.getvalue())
            self.assertEqual(data["status"], "incomplete")
            self.assertEqual(data["checks"]["devices"], "incomplete")


class TestRunPreflightAggregation(unittest.TestCase):
    def test_fail_takes_precedence_over_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "DroidHub.app"
            dh = app_dir / "Contents" / "MacOS" / "DroidHub"
            adb = app_dir / "Contents" / "Resources" / "tools" / "adb"
            dh.parent.mkdir(parents=True, exist_ok=True)
            adb.parent.mkdir(parents=True, exist_ok=True)
            dh.write_text("#!/bin/sh\nexit 0\n")
            dh.chmod(0o755)
            adb.write_text("#!/bin/sh\nexit 0\n")
            adb.chmod(0o755)

            ws_dir = Path(td) / "workspace"
            ws_dir.mkdir()

            # ps returns returncode 1 -> desktop_pid: incomplete
            # platform_system="Linux" -> platform: fail
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr="process not found",
                )
                report = run_preflight(
                    app=app_dir,
                    workspace=ws_dir,
                    desktop_pid=123,
                    devices=["DEV1", "DEV2"],
                    platform_system="Linux",
                    platform_machine="x86_64",
                )
                self.assertEqual(report["checks"]["platform"], "fail")
                self.assertEqual(report["checks"]["app"], "ok")
                self.assertEqual(report["checks"]["desktop_pid"], "incomplete")
                self.assertEqual(report["status"], "fail")
                self.assertEqual(report["overall_status"], "fail")

    def test_incomplete_overall_when_no_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "DroidHub.app"
            dh = app_dir / "Contents" / "MacOS" / "DroidHub"
            adb = app_dir / "Contents" / "Resources" / "tools" / "adb"
            dh.parent.mkdir(parents=True, exist_ok=True)
            adb.parent.mkdir(parents=True, exist_ok=True)
            dh.write_text("#!/bin/sh\nexit 0\n")
            dh.chmod(0o755)
            adb.write_text("#!/bin/sh\nexit 0\n")
            adb.chmod(0o755)

            ws_dir = Path(td) / "workspace"
            ws_dir.mkdir()

            pid = 999
            def mock_run(cmd, *args, **kwargs):
                if cmd[0] == "ps":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout=f"{pid} {dh.resolve()}\n",
                        stderr="",
                    )
                # adb timeout -> incomplete
                raise subprocess.TimeoutExpired(cmd="adb", timeout=5.0)

            with patch("subprocess.run", side_effect=mock_run):
                report = run_preflight(
                    app=app_dir,
                    workspace=ws_dir,
                    desktop_pid=pid,
                    devices=["DEV1", "DEV2"],
                    platform_system="Darwin",
                    platform_machine="arm64",
                )
                self.assertEqual(report["checks"]["platform"], "ok")
                self.assertEqual(report["checks"]["app"], "ok")
                self.assertEqual(report["checks"]["desktop_pid"], "ok")
                self.assertEqual(report["checks"]["workspace"], "ok")
                self.assertEqual(report["checks"]["devices"], "incomplete")
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["overall_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
