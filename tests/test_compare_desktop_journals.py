"""
Tests for scripts/compare_desktop_journals.py.

Covers:
- Empty databases with valid schema
- Progress of active runs and targets (queued -> running -> terminal)
- Losses (run lost, target lost, result lost, result changed)
- JSON with differing field orders, formatting, and nested structures
- Corrupted database and PRAGMA quick_check failures
- Missing file paths
- SQLite URI quoting with spaces, ?, #, %, and other special characters
- New runs after the copy
- Schema variations (targets.result vs run_targets.result vs targets.result_json)
- Ensuring device_id and raw results are never output in reports
- CLI execution via subprocess with text and --json flags
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure scripts module can be imported
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.compare_desktop_journals import (
    compare_journals,
    format_text_report,
    make_sqlite_ro_uri,
    normalize_json,
)

# Standard contract DDL with targets.result
DDL_TARGETS = """
CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    scenario      TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    params        TEXT NOT NULL,
    parent_run_id TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    created_at    INTEGER NOT NULL
);

CREATE TABLE targets (
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    device_id TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'queued',
    result    TEXT,
    PRIMARY KEY (run_id, device_id)
);
"""

# Alternative contract DDL with run_targets table
DDL_RUN_TARGETS = """
CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    scenario      TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    params        TEXT NOT NULL,
    parent_run_id TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    created_at    INTEGER NOT NULL
);

CREATE TABLE run_targets (
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    device_id TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'queued',
    result    TEXT,
    PRIMARY KEY (run_id, device_id)
);
"""

# Alternative contract DDL with targets.result_json
DDL_RESULT_JSON = """
CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    scenario      TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    params        TEXT NOT NULL,
    parent_run_id TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    created_at    INTEGER NOT NULL
);

CREATE TABLE targets (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    device_id   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    result_json TEXT,
    PRIMARY KEY (run_id, device_id)
);
"""


def create_db(path: Path, ddl: str = DDL_TARGETS) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    conn.commit()
    return conn


def insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    scenario: str = "smoke",
    request_id: str | None = None,
    params: str = '{"route": "main", "parallel": 1}',
    parent_run_id: str | None = None,
    status: str = "queued",
    created_at: int = 1700000000000,
) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, scenario, request_id, params, parent_run_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, scenario, request_id or run_id, params, parent_run_id, status, created_at),
    )
    conn.commit()


def insert_target(
    conn: sqlite3.Connection,
    run_id: str,
    device_id: str,
    *,
    table: str = "targets",
    status: str = "queued",
    result_col: str = "result",
    result: str | None = None,
) -> None:
    conn.execute(
        f"INSERT INTO {table} (run_id, device_id, status, {result_col}) "
        "VALUES (?, ?, ?, ?)",
        (run_id, device_id, status, result),
    )
    conn.commit()


# ===========================================================================
# 1. Empty database with schema
# ===========================================================================

def test_empty_databases_with_schema(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"
    create_db(b_path).close()
    create_db(a_path).close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["reason_codes"] == []
    assert report["aggregates"]["before_runs"] == 0
    assert report["aggregates"]["after_runs"] == 0
    assert report["aggregates"]["violations_count"] == 0


# ===========================================================================
# 2. Progress of active runs and targets
# ===========================================================================

def test_progress_active_run_to_running(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="queued")
    insert_target(b_conn, "run-1", "dev-1", status="queued")
    insert_target(b_conn, "run-1", "dev-2", status="queued")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="running")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    insert_target(a_conn, "run-1", "dev-2", status="running")
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["runs_progressed"] == 1
    assert report["aggregates"]["targets_progressed"] == 2
    assert report["aggregates"]["violations_count"] == 0


def test_progress_active_run_to_terminal(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="running")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    insert_target(b_conn, "run-1", "dev-2", status="running")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    insert_target(a_conn, "run-1", "dev-2", status="succeeded", result='{"code": 0}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["runs_progressed"] == 1
    assert report["aggregates"]["targets_terminal_preserved"] == 1
    assert report["aggregates"]["targets_progressed"] == 1
    assert report["aggregates"]["violations_count"] == 0


# ===========================================================================
# 3. Losses and regressions
# ===========================================================================

def test_loss_of_run(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1")
    insert_run(b_conn, "run-2")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1")
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RUN_LOST" in report["reason_codes"]
    assert report["aggregates"]["violations_count"] == 1


def test_loss_of_target(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1")
    insert_target(b_conn, "run-1", "dev-1")
    insert_target(b_conn, "run-1", "dev-2")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1")
    insert_target(a_conn, "run-1", "dev-1")
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "TARGET_LOST" in report["reason_codes"]


def test_target_set_changed_added_to_old_run(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1")
    insert_target(b_conn, "run-1", "dev-1")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1")
    insert_target(a_conn, "run-1", "dev-1")
    insert_target(a_conn, "run-1", "dev-2")  # extra target added
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "TARGET_SET_CHANGED" in report["reason_codes"]


def test_loss_of_ready_result(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="succeeded")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result=None)
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RESULT_LOST" in report["reason_codes"]


def test_change_of_ready_result(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="succeeded")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result='{"code": 1}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RESULT_CHANGED" in report["reason_codes"]


def test_terminal_target_status_change(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="succeeded")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="failed", result='{"code": 0}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "TARGET_INVALID_STATUS_TRANSITION" in report["reason_codes"]


def test_terminal_run_status_change(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="succeeded")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="failed")
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RUN_INVALID_STATUS_TRANSITION" in report["reason_codes"]


def test_active_status_reversion(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="running")
    insert_target(b_conn, "run-1", "dev-1", status="running")
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="queued")
    insert_target(a_conn, "run-1", "dev-1", status="queued")
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RUN_INVALID_STATUS_TRANSITION" in report["reason_codes"]
    assert "TARGET_INVALID_STATUS_TRANSITION" in report["reason_codes"]


def test_run_metadata_alteration(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", scenario="smoke", params='{"parallel": 1}')
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", scenario="regression", params='{"parallel": 1}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RUN_METADATA_CHANGED" in report["reason_codes"]


# ===========================================================================
# 4. JSON normalization (different key ordering, spacing, types)
# ===========================================================================

def test_json_normalization_field_order_and_whitespace(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    json_b_params = """
    {
        "route": "main",
        "parallel": 2,
        "options": {"retry": true, "timeout": 30}
    }
    """
    json_a_params = """{"options": {"timeout": 30, "retry": true}, "parallel": 2, "route": "main"}"""

    json_b_result = """
    {
        "status": "succeeded",
        "steps": [
            {"name": "setup", "ok": true},
            {"name": "exec", "ok": true}
        ],
        "code": 0
    }
    """
    json_a_result = """{"code": 0, "steps": [{"ok": true, "name": "setup"}, {"name": "exec", "ok": true}], "status": "succeeded"}"""

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", params=json_b_params, status="succeeded")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result=json_b_result)
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", params=json_a_params, status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result=json_a_result)
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["violations_count"] == 0


def test_json_normalization_boolean_vs_number():
    assert normalize_json('{"val": true}') != normalize_json('{"val": 1}')
    assert normalize_json('{"val": false}') != normalize_json('{"val": 0}')


# ===========================================================================
# 5. Corrupted database & PRAGMA quick_check
# ===========================================================================

def test_corrupt_database_quick_check_failure(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    create_db(b_path).close()
    # Write garbage into after.sqlite3
    a_path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 2048)

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "DB_CORRUPT" in report["reason_codes"]


def test_corrupt_database_completely_invalid_file(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_path.write_text("not a sqlite database")
    create_db(a_path).close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "DB_CORRUPT" in report["reason_codes"]


# ===========================================================================
# 6. Missing paths
# ===========================================================================

def test_missing_database_paths(tmp_path: Path):
    b_path = tmp_path / "missing_before.sqlite3"
    a_path = tmp_path / "missing_after.sqlite3"

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "MISSING_PATH" in report["reason_codes"]
    assert report["aggregates"]["violations_count"] == 2


def test_one_missing_database_path(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "missing_after.sqlite3"
    create_db(b_path).close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "MISSING_PATH" in report["reason_codes"]
    assert report["aggregates"]["violations_count"] == 1


# ===========================================================================
# 7. SQLite URI quoting with spaces, ?, #, %, etc.
# ===========================================================================

def test_sqlite_uri_quoting_complex_paths(tmp_path: Path):
    b_dir = tmp_path / "dir with spaces #1? & more"
    b_dir.mkdir(parents=True)
    b_path = b_dir / "before%20journal#1?.sqlite3"
    a_path = b_dir / "after%20journal#2?.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="succeeded")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["violations_count"] == 0


# ===========================================================================
# 8. New runs allowed after copy
# ===========================================================================

def test_new_runs_allowed_after_copy(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "run-1", status="succeeded")
    insert_target(b_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "run-1", status="succeeded")
    insert_target(a_conn, "run-1", "dev-1", status="succeeded", result='{"code": 0}')
    # Add new run
    insert_run(a_conn, "run-new-2", status="queued")
    insert_target(a_conn, "run-new-2", "dev-2", status="queued")
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["before_runs"] == 1
    assert report["aggregates"]["after_runs"] == 2
    assert report["aggregates"]["new_runs"] == 1


# ===========================================================================
# 9. Schema contract variations (targets.result vs run_targets.result vs targets.result_json)
# ===========================================================================

def test_contract_schema_targets_result(tmp_path: Path):
    """Primary contract: table 'targets', column 'result'."""
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path, ddl=DDL_TARGETS)
    insert_run(b_conn, "r1", status="succeeded")
    insert_target(b_conn, "r1", "d1", table="targets", result_col="result", status="succeeded", result='{"ok": true}')
    b_conn.close()

    a_conn = create_db(a_path, ddl=DDL_TARGETS)
    insert_run(a_conn, "r1", status="succeeded")
    insert_target(a_conn, "r1", "d1", table="targets", result_col="result", status="succeeded", result='{"ok": true}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["targets_terminal_preserved"] == 1


def test_contract_schema_run_targets_result(tmp_path: Path):
    """Alternate table form: table 'run_targets', column 'result'."""
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path, ddl=DDL_RUN_TARGETS)
    insert_run(b_conn, "r1", status="succeeded")
    insert_target(b_conn, "r1", "d1", table="run_targets", result_col="result", status="succeeded", result='{"ok": true}')
    b_conn.close()

    a_conn = create_db(a_path, ddl=DDL_RUN_TARGETS)
    insert_run(a_conn, "r1", status="succeeded")
    insert_target(a_conn, "r1", "d1", table="run_targets", result_col="result", status="succeeded", result='{"ok": true}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["targets_terminal_preserved"] == 1


def test_contract_schema_targets_result_json(tmp_path: Path):
    """Alternate column form: table 'targets', column 'result_json'."""
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path, ddl=DDL_RESULT_JSON)
    insert_run(b_conn, "r1", status="succeeded")
    insert_target(b_conn, "r1", "d1", table="targets", result_col="result_json", status="succeeded", result='{"ok": true}')
    b_conn.close()

    a_conn = create_db(a_path, ddl=DDL_RESULT_JSON)
    insert_run(a_conn, "r1", status="succeeded")
    insert_target(a_conn, "r1", "d1", table="targets", result_col="result_json", status="succeeded", result='{"ok": true}')
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is True
    assert report["aggregates"]["targets_terminal_preserved"] == 1


def test_missing_schema_table(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    # Only create runs table, omit targets
    conn = sqlite3.connect(b_path)
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, scenario TEXT, request_id TEXT, params TEXT, parent_run_id TEXT, status TEXT, created_at INTEGER)")
    conn.commit()
    conn.close()
    create_db(a_path).close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "SCHEMA_MISSING_TABLE" in report["reason_codes"]


# ===========================================================================
# 10. Privacy: NO device_id or raw results leaked in reports
# ===========================================================================

def test_no_device_id_or_raw_result_in_output(tmp_path: Path):
    secret_device = "SECRET_DEVICE_ID_987654"
    secret_payload = "SUPER_SECRET_TOKEN_ABCD_1234"

    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"

    b_conn = create_db(b_path)
    insert_run(b_conn, "r1", status="succeeded")
    insert_target(
        b_conn,
        "r1",
        secret_device,
        status="succeeded",
        result=f'{{"token": "{secret_payload}"}}',
    )
    b_conn.close()

    a_conn = create_db(a_path)
    insert_run(a_conn, "r1", status="succeeded")
    insert_target(
        a_conn,
        "r1",
        secret_device,
        status="succeeded",
        result='{"token": "different_payload"}',
    )
    a_conn.close()

    report = compare_journals(b_path, a_path)
    assert report["ok"] is False
    assert "RESULT_CHANGED" in report["reason_codes"]

    # Verify JSON output
    json_str = json.dumps(report)
    assert secret_device not in json_str, "device_id was leaked in JSON output!"
    assert secret_payload not in json_str, "raw result was leaked in JSON output!"

    # Verify text report output
    text_str = format_text_report(report)
    assert secret_device not in text_str, "device_id was leaked in text output!"
    assert secret_payload not in text_str, "raw result was leaked in text output!"


# ===========================================================================
# 11. CLI execution tests
# ===========================================================================

def test_cli_execution_success(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "after.sqlite3"
    create_db(b_path).close()
    create_db(a_path).close()

    # Plain text CLI
    proc = subprocess.run(
        [sys.executable, str(ROOT_DIR / "scripts" / "compare_desktop_journals.py"), str(b_path), str(a_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "Journal Comparison: OK" in proc.stdout

    # JSON CLI
    proc_json = subprocess.run(
        [sys.executable, str(ROOT_DIR / "scripts" / "compare_desktop_journals.py"), str(b_path), str(a_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert proc_json.returncode == 0
    parsed = json.loads(proc_json.stdout)
    assert parsed["ok"] is True


def test_cli_execution_failure(tmp_path: Path):
    b_path = tmp_path / "before.sqlite3"
    a_path = tmp_path / "missing_after.sqlite3"
    create_db(b_path).close()

    proc = subprocess.run(
        [sys.executable, str(ROOT_DIR / "scripts" / "compare_desktop_journals.py"), str(b_path), str(a_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    parsed = json.loads(proc_json := proc.stdout)
    assert parsed["ok"] is False
    assert "MISSING_PATH" in parsed["reason_codes"]
