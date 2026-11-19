#!/usr/bin/env python3
"""
Safely compare DroidHub Desktop SQLite journals for upgrade acceptance.

Standalone CLI tool using only the Python standard library.
Enforces read-only access with SQLite URI mode=ro, verifies DB integrity,
and validates that old runs and targets retain terminal status, results,
and metadata while permitting active progress and new runs.

Output includes aggregates and reason codes without printing device IDs or raw results.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Active statuses that are permitted to transition towards terminal
ACTIVE_STATUSES = frozenset({"queued", "running"})

# Canonical standard reason codes
REASON_DESCRIPTIONS: dict[str, str] = {
    "MISSING_PATH": "Database path does not exist or is not a file",
    "DB_CORRUPT": "Database integrity check failed or database is corrupt",
    "SCHEMA_MISSING_TABLE": "Required table is missing from database schema",
    "SCHEMA_MISSING_COLUMN": "Required column is missing from database schema",
    "RUN_LOST": "Existing run was removed or lost in after database",
    "RUN_METADATA_CHANGED": "Existing run metadata was altered",
    "RUN_INVALID_STATUS_TRANSITION": "Illegal run status transition",
    "TARGET_LOST": "Existing target was removed or lost in after database",
    "TARGET_SET_CHANGED": "Target set altered for existing run",
    "TARGET_INVALID_STATUS_TRANSITION": "Illegal target status transition",
    "RESULT_LOST": "Previously recorded terminal result was lost or cleared",
    "RESULT_CHANGED": "Previously recorded terminal result was altered",
}


class SchemaInspectionError(Exception):
    """Raised when database schema does not meet expectations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def make_sqlite_ro_uri(path: str | Path) -> str:
    """Return a sqlite3 URI in read-only mode (mode=ro) with proper quoting.

    Path.as_uri() safely percent-encodes spaces, ?, #, %, and other characters.
    """
    resolved = Path(path).resolve()
    base_uri = resolved.as_uri()
    return f"{base_uri}?mode=ro"


def open_ro_connection(path: str | Path) -> sqlite3.Connection:
    """Open a read-only SQLite connection using URI mode=ro."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Database file does not exist: {resolved}")
    uri = make_sqlite_ro_uri(resolved)
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def run_quick_check(conn: sqlite3.Connection) -> bool:
    """Run PRAGMA quick_check without repairs or writes.

    Returns True if database passes integrity check, False otherwise.
    """
    try:
        cur = conn.execute("PRAGMA quick_check")
        rows = cur.fetchall()
        return len(rows) == 1 and rows[0][0] == "ok"
    except sqlite3.DatabaseError:
        return False


def inspect_schema(conn: sqlite3.Connection) -> dict[str, str]:
    """Inspect and validate schema for runs and targets tables.

    Contract specifies table 'runs' and table 'targets' with column 'result'.
    Both 'targets' and 'run_targets' table forms are supported.
    Both 'result' and 'result_json' column forms are supported.
    """
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}

    if "runs" not in tables:
        raise SchemaInspectionError(
            "SCHEMA_MISSING_TABLE",
            "Required table 'runs' is missing from schema",
        )

    targets_table = None
    if "targets" in tables:
        targets_table = "targets"
    elif "run_targets" in tables:
        targets_table = "run_targets"
    else:
        raise SchemaInspectionError(
            "SCHEMA_MISSING_TABLE",
            "Required targets table ('targets' or 'run_targets') is missing from schema",
        )

    # Validate columns in 'runs'
    cur = conn.execute("PRAGMA table_info(runs)")
    runs_cols = {row[1] for row in cur.fetchall()}
    expected_runs_cols = {
        "run_id", "scenario", "request_id", "params",
        "parent_run_id", "status", "created_at"
    }
    missing_runs = expected_runs_cols - runs_cols
    if missing_runs:
        raise SchemaInspectionError(
            "SCHEMA_MISSING_COLUMN",
            f"Table 'runs' missing required column(s): {sorted(missing_runs)}",
        )

    # Validate columns in targets table
    cur = conn.execute(f"PRAGMA table_info({targets_table})")
    targets_cols = {row[1] for row in cur.fetchall()}
    base_targets_cols = {"run_id", "device_id", "status"}
    missing_targets = base_targets_cols - targets_cols
    if missing_targets:
        raise SchemaInspectionError(
            "SCHEMA_MISSING_COLUMN",
            f"Table '{targets_table}' missing required column(s): {sorted(missing_targets)}",
        )

    result_col = None
    if "result" in targets_cols:
        result_col = "result"
    elif "result_json" in targets_cols:
        result_col = "result_json"
    else:
        raise SchemaInspectionError(
            "SCHEMA_MISSING_COLUMN",
            f"Table '{targets_table}' missing result column ('result' or 'result_json')",
        )

    return {
        "targets_table": targets_table,
        "result_col": result_col,
    }


def load_journal_data(
    conn: sqlite3.Connection,
    schema: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    """Load runs and targets from database.

    Returns:
      runs: dict[run_id -> run_dict]
      targets: dict[run_id -> dict[device_id -> target_dict]]
    """
    targets_table = schema["targets_table"]
    result_col = schema["result_col"]

    cur = conn.execute(
        "SELECT run_id, scenario, request_id, params, parent_run_id, status, created_at "
        "FROM runs"
    )
    runs: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        r = dict(row)
        runs[r["run_id"]] = r

    cur = conn.execute(
        f"SELECT run_id, device_id, status, {result_col} AS result "
        f"FROM {targets_table}"
    )
    targets: dict[str, dict[str, dict[str, Any]]] = {}
    for row in cur.fetchall():
        rid = row["run_id"]
        dev = row["device_id"]
        if rid not in targets:
            targets[rid] = {}
        targets[rid][dev] = {
            "status": row["status"],
            "result": row["result"],
        }

    return runs, targets


def _canonicalize_value(obj: Any) -> Any:
    """Recursively canonicalize parsed structures for semantic comparison."""
    if isinstance(obj, bool):
        return ("__bool__", obj)
    if isinstance(obj, (int, float)):
        return ("__num__", obj)
    if isinstance(obj, dict):
        return {str(k): _canonicalize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonicalize_value(x) for x in obj]
    return obj


def normalize_json(val: Any) -> Any:
    """Normalize a JSON or raw value by semantic meaning.

    Handles whitespace, differing dictionary key orders, nested structures,
    and distinguishes booleans from numbers.
    Empty string or NULL normalizes to None.
    """
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s == "null":
            return None
        try:
            parsed = json.loads(s)
            return _canonicalize_value(parsed)
        except (json.JSONDecodeError, ValueError):
            return s
    return _canonicalize_value(val)


def is_active(status: str | None) -> bool:
    """Return True if status is queued or running."""
    return status in ACTIVE_STATUSES


def is_terminal(status: str | None) -> bool:
    """Return True if status is terminal (not queued and not running)."""
    return bool(status and status not in ACTIVE_STATUSES)


def compare_journals(
    before_path: str | Path,
    after_path: str | Path,
) -> dict[str, Any]:
    """Compare BEFORE and AFTER SQLite journal databases safely.

    Returns a report dictionary containing:
      - ok: bool
      - reason_codes: list[str]
      - reasons: list[dict(code, count, message)]
      - aggregates: dict[str, int]
    """
    violations: list[str] = []

    # 1. Validate file existence
    b_path = Path(before_path).resolve()
    a_path = Path(after_path).resolve()

    if not b_path.is_file():
        violations.append("MISSING_PATH")
    if not a_path.is_file():
        violations.append("MISSING_PATH")

    if violations:
        code_counts = Counter(violations)
        return {
            "ok": False,
            "reason_codes": sorted(code_counts.keys()),
            "reasons": [
                {
                    "code": code,
                    "count": count,
                    "message": REASON_DESCRIPTIONS.get(code, "Validation error"),
                }
                for code, count in sorted(code_counts.items())
            ],
            "aggregates": {
                "before_runs": 0,
                "after_runs": 0,
                "new_runs": 0,
                "matching_runs": 0,
                "runs_progressed": 0,
                "runs_unchanged": 0,
                "before_targets": 0,
                "after_targets": 0,
                "targets_terminal_preserved": 0,
                "targets_progressed": 0,
                "targets_active_preserved": 0,
                "violations_count": len(violations),
            },
        }

    # 2. Open read-only connections
    try:
        b_conn = open_ro_connection(b_path)
    except Exception:
        violations.append("DB_CORRUPT")
        b_conn = None

    try:
        a_conn = open_ro_connection(a_path)
    except Exception:
        violations.append("DB_CORRUPT")
        a_conn = None

    if violations or b_conn is None or a_conn is None:
        if b_conn:
            b_conn.close()
        if a_conn:
            a_conn.close()
        code_counts = Counter(violations)
        return {
            "ok": False,
            "reason_codes": sorted(code_counts.keys()),
            "reasons": [
                {
                    "code": code,
                    "count": count,
                    "message": REASON_DESCRIPTIONS.get(code, "Database error"),
                }
                for code, count in sorted(code_counts.items())
            ],
            "aggregates": {
                "before_runs": 0,
                "after_runs": 0,
                "new_runs": 0,
                "matching_runs": 0,
                "runs_progressed": 0,
                "runs_unchanged": 0,
                "before_targets": 0,
                "after_targets": 0,
                "targets_terminal_preserved": 0,
                "targets_progressed": 0,
                "targets_active_preserved": 0,
                "violations_count": len(violations),
            },
        }

    try:
        # 3. PRAGMA quick_check
        if not run_quick_check(b_conn):
            violations.append("DB_CORRUPT")
        if not run_quick_check(a_conn):
            violations.append("DB_CORRUPT")

        if violations:
            code_counts = Counter(violations)
            return {
                "ok": False,
                "reason_codes": sorted(code_counts.keys()),
                "reasons": [
                    {
                        "code": code,
                        "count": count,
                        "message": REASON_DESCRIPTIONS.get(code, "Integrity check failed"),
                    }
                    for code, count in sorted(code_counts.items())
                ],
                "aggregates": {
                    "before_runs": 0,
                    "after_runs": 0,
                    "new_runs": 0,
                    "matching_runs": 0,
                    "runs_progressed": 0,
                    "runs_unchanged": 0,
                    "before_targets": 0,
                    "after_targets": 0,
                    "targets_terminal_preserved": 0,
                    "targets_progressed": 0,
                    "targets_active_preserved": 0,
                    "violations_count": len(violations),
                },
            }

        # 4. Schema inspection
        try:
            b_schema = inspect_schema(b_conn)
        except SchemaInspectionError as exc:
            violations.append(exc.code)
            b_schema = None

        try:
            a_schema = inspect_schema(a_conn)
        except SchemaInspectionError as exc:
            violations.append(exc.code)
            a_schema = None

        if violations or b_schema is None or a_schema is None:
            code_counts = Counter(violations)
            return {
                "ok": False,
                "reason_codes": sorted(code_counts.keys()),
                "reasons": [
                    {
                        "code": code,
                        "count": count,
                        "message": REASON_DESCRIPTIONS.get(code, "Schema error"),
                    }
                    for code, count in sorted(code_counts.items())
                ],
                "aggregates": {
                    "before_runs": 0,
                    "after_runs": 0,
                    "new_runs": 0,
                    "matching_runs": 0,
                    "runs_progressed": 0,
                    "runs_unchanged": 0,
                    "before_targets": 0,
                    "after_targets": 0,
                    "targets_terminal_preserved": 0,
                    "targets_progressed": 0,
                    "targets_active_preserved": 0,
                    "violations_count": len(violations),
                },
            }

        # 5. Load data
        b_runs, b_targets = load_journal_data(b_conn, b_schema)
        a_runs, a_targets = load_journal_data(a_conn, a_schema)

    finally:
        b_conn.close()
        a_conn.close()

    # 6. Compare runs and targets
    b_run_ids = set(b_runs.keys())
    a_run_ids = set(a_runs.keys())

    matching_run_ids = b_run_ids & a_run_ids
    lost_run_ids = b_run_ids - a_run_ids
    new_run_ids = a_run_ids - b_run_ids

    for _ in lost_run_ids:
        violations.append("RUN_LOST")

    runs_progressed = 0
    runs_unchanged = 0

    total_b_targets = sum(len(devs) for devs in b_targets.values())
    total_a_targets = sum(len(devs) for devs in a_targets.values())

    targets_terminal_preserved = 0
    targets_progressed = 0
    targets_active_preserved = 0

    for rid in matching_run_ids:
        b_r = b_runs[rid]
        a_r = a_runs[rid]

        # Verify metadata: scenario, params, parent_run_id, created_at, request_id
        meta_ok = (
            b_r["scenario"] == a_r["scenario"]
            and normalize_json(b_r["params"]) == normalize_json(a_r["params"])
            and b_r["parent_run_id"] == a_r["parent_run_id"]
            and b_r["created_at"] == a_r["created_at"]
            and b_r["request_id"] == a_r["request_id"]
        )
        if not meta_ok:
            violations.append("RUN_METADATA_CHANGED")

        # Verify run status transition
        b_stat = b_r["status"]
        a_stat = a_r["status"]

        if is_terminal(b_stat):
            if a_stat != b_stat:
                violations.append("RUN_INVALID_STATUS_TRANSITION")
            else:
                runs_unchanged += 1
        elif is_active(b_stat):
            if b_stat == "running" and a_stat == "queued":
                # Cannot regress from running to queued
                violations.append("RUN_INVALID_STATUS_TRANSITION")
            else:
                if a_stat != b_stat:
                    runs_progressed += 1
                else:
                    runs_unchanged += 1
        else:
            # Unknown initial status: treat as requiring exact match
            if a_stat != b_stat:
                violations.append("RUN_INVALID_STATUS_TRANSITION")
            else:
                runs_unchanged += 1

        # Verify target membership and targets
        b_devs = b_targets.get(rid, {})
        a_devs = a_targets.get(rid, {})

        b_dev_keys = set(b_devs.keys())
        a_dev_keys = set(a_devs.keys())

        missing_devs = b_dev_keys - a_dev_keys
        extra_devs = a_dev_keys - b_dev_keys

        for _ in missing_devs:
            violations.append("TARGET_LOST")
        for _ in extra_devs:
            violations.append("TARGET_SET_CHANGED")

        for dev in b_dev_keys & a_dev_keys:
            b_t = b_devs[dev]
            a_t = a_devs[dev]

            b_t_stat = b_t["status"]
            a_t_stat = a_t["status"]

            b_res_norm = normalize_json(b_t["result"])
            a_res_norm = normalize_json(a_t["result"])

            if is_terminal(b_t_stat):
                # Terminal status must be preserved identically
                if a_t_stat != b_t_stat:
                    violations.append("TARGET_INVALID_STATUS_TRANSITION")

                # Terminal result must be preserved identically
                if b_res_norm is not None:
                    if a_res_norm is None:
                        violations.append("RESULT_LOST")
                    elif a_res_norm != b_res_norm:
                        violations.append("RESULT_CHANGED")
                    else:
                        if a_t_stat == b_t_stat:
                            targets_terminal_preserved += 1
                else:
                    if a_res_norm is not None:
                        violations.append("RESULT_CHANGED")
                    else:
                        if a_t_stat == b_t_stat:
                            targets_terminal_preserved += 1

            elif is_active(b_t_stat):
                # Active status may progress to running or terminal, never revert
                if b_t_stat == "running" and a_t_stat == "queued":
                    violations.append("TARGET_INVALID_STATUS_TRANSITION")

                # If an active target already had a ready result, it cannot be lost or changed
                if b_res_norm is not None:
                    if a_res_norm is None:
                        violations.append("RESULT_LOST")
                    elif a_res_norm != b_res_norm:
                        violations.append("RESULT_CHANGED")

                # Track progression vs preservation
                if a_t_stat != b_t_stat or (b_res_norm is None and a_res_norm is not None):
                    targets_progressed += 1
                else:
                    targets_active_preserved += 1

            else:
                # Unknown status: enforce exact match
                if a_t_stat != b_t_stat:
                    violations.append("TARGET_INVALID_STATUS_TRANSITION")
                if b_res_norm != a_res_norm:
                    violations.append("RESULT_CHANGED")

    code_counts = Counter(violations)
    reasons = [
        {
            "code": code,
            "count": count,
            "message": REASON_DESCRIPTIONS.get(code, "Validation error"),
        }
        for code, count in sorted(code_counts.items())
    ]

    return {
        "ok": len(violations) == 0,
        "reason_codes": sorted(code_counts.keys()),
        "reasons": reasons,
        "aggregates": {
            "before_runs": len(b_runs),
            "after_runs": len(a_runs),
            "new_runs": len(new_run_ids),
            "matching_runs": len(matching_run_ids),
            "runs_progressed": runs_progressed,
            "runs_unchanged": runs_unchanged,
            "before_targets": total_b_targets,
            "after_targets": total_a_targets,
            "targets_terminal_preserved": targets_terminal_preserved,
            "targets_progressed": targets_progressed,
            "targets_active_preserved": targets_active_preserved,
            "violations_count": len(violations),
        },
    }


def format_text_report(report: dict[str, Any]) -> str:
    """Format report into human-readable text without device_id or raw result."""
    lines: list[str] = []
    status_str = "OK" if report["ok"] else "FAILED"
    lines.append(f"Journal Comparison: {status_str}")
    lines.append("-" * 40)
    lines.append("Aggregates:")
    for k, v in report.get("aggregates", {}).items():
        lines.append(f"  {k}: {v}")

    reasons = report.get("reasons", [])
    if reasons:
        lines.append("-" * 40)
        lines.append("Violations / Reason Codes:")
        for r in reasons:
            lines.append(f"  [{r['code']}] count={r['count']}: {r['message']}")
    else:
        lines.append("-" * 40)
        lines.append("Reason Codes: (none)")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for compare_desktop_journals."""
    parser = argparse.ArgumentParser(
        description="Safely compare DroidHub Desktop SQLite journals for upgrade acceptance."
    )
    parser.add_argument("before", help="Path to BEFORE SQLite database")
    parser.add_argument("after", help="Path to AFTER SQLite database")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output report in JSON format",
    )

    args = parser.parse_args(argv)

    report = compare_journals(args.before, args.after)

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text_report(report))

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
