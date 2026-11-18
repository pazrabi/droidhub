"""
Tests for desktop.run_cli
=========================

All production dependencies (desktop.link_storage, desktop.run_history,
desktop.readiness_runtime) are intentionally absent.  Every test injects
the callables it needs; no stubs, no sys.modules patching.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from typing import Any

import pytest

from desktop.run_cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_DB = "/tmp/fake-runs.sqlite3"

_SAMPLE_RUNS = [
    {"run_id": "r001", "status": "completed", "started_at": "2026-11-01T10:00:00Z"},
    {"run_id": "r002", "status": "failed", "started_at": "2026-11-02T11:00:00Z"},
]

_SAMPLE_RUN = {
    "schema_version": 1,
    "run_id": "r001",
    "status": "completed",
    "started_at": "2026-11-01T10:00:00Z",
    "targets": [
        {"serial": "AAAA1111", "status": "ok", "battery": 85},
    ],
}

_SAMPLE_REPORT = {
    "schema_version": 1,
    "run_id": "chk-999",
    "status": "completed",
    "targets": [
        {"serial": "AAAA1111", "status": "ok", "battery": 90},
        {"serial": "BBBB2222", "status": "ok", "battery": 75},
    ],
}

_PARTIAL_REPORT = {
    "schema_version": 1,
    "run_id": "chk-partial",
    "status": "partial",
    "targets": [
        {"serial": "AAAA1111", "status": "ok", "battery": 90},
        {"serial": "BBBB2222", "status": "failed", "battery": 5},
    ],
}


def _list_runs_ok(path, *, limit=50, before=None):
    return _SAMPLE_RUNS[:limit]


def _list_runs_empty(path, *, limit=50, before=None):
    return []


def _get_run_ok(path, run_id):
    if run_id == "r001":
        return _SAMPLE_RUN
    return None


def _get_run_none(path, run_id):
    return None


def _run_check_ok(path, serials, *, min_battery):
    return dict(_SAMPLE_REPORT)


def _run_check_partial(path, serials, *, min_battery):
    return dict(_PARTIAL_REPORT)


def _run_check_error(path, serials, *, min_battery):
    raise RuntimeError("executor blew up")


def _capture(argv, **kwargs):
    """Run main(argv, **kwargs) capturing stdout/stderr; return (rc, out, err)."""
    out_buf = StringIO()
    err_buf = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = out_buf
    sys.stderr = err_buf
    try:
        rc = main(argv, db_path_fn=lambda: FAKE_DB, **kwargs)
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# history tests
# ---------------------------------------------------------------------------


class TestHistory:
    def test_text_output(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "history"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 0
        assert "r001" in out
        assert "r002" in out
        assert err == ""

    def test_json_output_schema_version(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "history"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 0
        data = json.loads(out)
        assert data["schema_version"] == 1
        assert isinstance(data["runs"], list)
        assert len(data["runs"]) == 2

    def test_limit_respected(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "history", "--limit", "1"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 0
        data = json.loads(out)
        assert len(data["runs"]) == 1

    def test_empty_history_text(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "history"],
            list_runs_fn=_list_runs_empty,
        )
        assert rc == 0
        assert "No runs found" in out

    def test_empty_history_json(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "history"],
            list_runs_fn=_list_runs_empty,
        )
        assert rc == 0
        data = json.loads(out)
        assert data["runs"] == []

    def test_limit_zero_is_bad_args(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "history", "--limit", "0"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 1
        assert "error" in err.lower()

    def test_limit_negative_is_bad_args(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "history", "--limit", "-5"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 1

    def test_json_stdout_clean_no_diagnostics(self):
        """JSON stdout must not contain diagnostics."""
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "history"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 0
        # stdout is valid JSON
        json.loads(out)
        # stderr is empty on success
        assert err == ""


# ---------------------------------------------------------------------------
# status tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_text_output(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "status", "r001"],
            get_run_fn=_get_run_ok,
        )
        assert rc == 0
        assert "r001" in out

    def test_json_output(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "status", "r001"],
            get_run_fn=_get_run_ok,
        )
        assert rc == 0
        data = json.loads(out)
        assert data["schema_version"] == 1
        assert data["run_id"] == "r001"
        assert data["status"] == "completed"

    def test_not_found_exit_2(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "status", "doesnotexist"],
            get_run_fn=_get_run_none,
        )
        assert rc == 2
        assert "not found" in err.lower()

    def test_not_found_no_json_on_stdout(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "status", "nope"],
            get_run_fn=_get_run_none,
        )
        assert rc == 2
        assert out.strip() == ""

    def test_requires_run_id(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--database", FAKE_DB, "status"], db_path_fn=lambda: FAKE_DB)
        assert exc.value.code != 0

    def test_json_schema_version(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "status", "r001"],
            get_run_fn=_get_run_ok,
        )
        data = json.loads(out)
        assert data["schema_version"] == 1


# ---------------------------------------------------------------------------
# check tests
# ---------------------------------------------------------------------------


class TestCheck:
    def test_success_text(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--device", "BBBB2222",
                "--min-battery", "20",
                "--confirm", "AAAA1111", "BBBB2222",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 0
        assert "chk-999" in out
        assert "completed" in out

    def test_success_json(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "--json",
                "check",
                "--device", "AAAA1111",
                "--device", "BBBB2222",
                "--min-battery", "20",
                "--confirm", "AAAA1111", "BBBB2222",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 0
        data = json.loads(out)
        assert data["schema_version"] == 1
        assert data["run_id"] == "chk-999"
        assert data["status"] == "completed"

    def test_partial_report_returned_exit_0(self):
        """A partial/failed/cancelled report is still returned with exit 0."""
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "--json",
                "check",
                "--device", "AAAA1111",
                "--device", "BBBB2222",
                "--min-battery", "20",
                "--confirm", "AAAA1111", "BBBB2222",
            ],
            run_check_fn=_run_check_partial,
        )
        assert rc == 0
        data = json.loads(out)
        assert data["status"] == "partial"
        assert data["schema_version"] == 1

    def test_executor_error_exit_3(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--min-battery", "20",
                "--confirm", "AAAA1111",
            ],
            run_check_fn=_run_check_error,
        )
        assert rc == 3
        assert "executor" in err.lower() or "error" in err.lower()

    def test_no_device_requires_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(
                ["--database", FAKE_DB, "check", "--min-battery", "20", "--confirm", "X"],
                db_path_fn=lambda: FAKE_DB,
                run_check_fn=_run_check_ok,
            )
        assert exc.value.code != 0

    def test_duplicate_device_exit_1(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--device", "AAAA1111",
                "--min-battery", "20",
                "--confirm", "AAAA1111",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 1
        assert "duplicate" in err.lower()

    def test_min_battery_too_high(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--min-battery", "101",
                "--confirm", "AAAA1111",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 1
        assert "min-battery" in err or "101" in err

    def test_min_battery_negative(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--min-battery", "-1",
                "--confirm", "AAAA1111",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 1

    def test_confirm_mismatch_exit_1(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--device", "BBBB2222",
                "--min-battery", "20",
                "--confirm", "AAAA1111",  # missing BBBB2222
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 1
        assert "confirm" in err.lower()

    def test_confirm_extra_serial_exit_1(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--min-battery", "20",
                "--confirm", "AAAA1111", "EXTRA",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 1

    def test_confirm_order_independent(self):
        """--confirm in reverse order is still valid."""
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--device", "BBBB2222",
                "--min-battery", "20",
                "--confirm", "BBBB2222", "AAAA1111",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 0

    def test_json_stdout_clean_on_success(self):
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "--json",
                "check",
                "--device", "AAAA1111",
                "--min-battery", "20",
                "--confirm", "AAAA1111",
            ],
            run_check_fn=lambda p, s, *, min_battery: {
                "schema_version": 1, "run_id": "x1", "status": "completed", "targets": [],
            },
        )
        assert rc == 0
        json.loads(out)  # must be valid JSON

    def test_diagnostics_go_to_stderr(self):
        """The 'running check' message must appear on stderr, not stdout."""
        rc, out, err = _capture(
            [
                "--database", FAKE_DB,
                "check",
                "--device", "AAAA1111",
                "--min-battery", "20",
                "--confirm", "AAAA1111",
            ],
            run_check_fn=_run_check_ok,
        )
        assert rc == 0
        # diagnostic "Running readiness check …" goes to stderr
        assert "readiness" in err.lower() or "running" in err.lower()

    def test_no_confirm_requires_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(
                ["--database", FAKE_DB, "check", "--device", "X", "--min-battery", "20"],
                db_path_fn=lambda: FAKE_DB,
                run_check_fn=_run_check_ok,
            )
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Global option tests
# ---------------------------------------------------------------------------


class TestGlobalOptions:
    def test_missing_command_exits_nonzero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--database", FAKE_DB], db_path_fn=lambda: FAKE_DB)
        assert exc.value.code != 0

    def test_database_path_passed_to_handler(self):
        """The injected list_runs receives the --database path."""
        seen = {}

        def capture_list(path, *, limit=50, before=None):
            seen["path"] = path
            return []

        _capture(
            ["--database", "/custom/path.db", "history"],
            list_runs_fn=capture_list,
        )
        assert seen.get("path") == "/custom/path.db"

    def test_json_flag_produces_valid_json(self):
        rc, out, err = _capture(
            ["--database", FAKE_DB, "--json", "history"],
            list_runs_fn=_list_runs_ok,
        )
        assert rc == 0
        data = json.loads(out)
        assert "schema_version" in data

    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"], db_path_fn=lambda: FAKE_DB)
        assert exc.value.code == 0

    def test_subcommand_help_exits_zero(self, capsys):
        for cmd in ("history", "status", "check"):
            with pytest.raises(SystemExit) as exc:
                main([cmd, "--help"], db_path_fn=lambda: FAKE_DB)
            assert exc.value.code == 0, f"{cmd} --help exited with {exc.value.code}"

    def test_unknown_command_exits_nonzero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--database", FAKE_DB, "frobnicate"], db_path_fn=lambda: FAKE_DB)
        assert exc.value.code != 0
