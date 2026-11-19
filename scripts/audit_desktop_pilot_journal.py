#!/usr/bin/env python3
"""Autonomous stdlib utility for auditing isolated DroidHub desktop USB pilot artifacts.

Compares terminal status aggregates from a pilot event journal (JSONL) against
an SQLite snapshot database containing runs(run_id, scenario, status).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from typing import Any, Optional
from urllib.request import pathname2url

TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "partial", "cancelled")
VALID_EVENT_TYPES: set[str] = {"start", "sample", "run", "disconnect", "reconnect", "shutdown"}
EXPECTED_SCENARIO: str = "usb_diagnostics"

LIMITATION_NOTE: str = (
    "Сопоставление агрегатов терминальных статусов не доказывает поштучное соответствие "
    "конкретных запусков или совпадение run_id, так как записи журнала не содержат run_id. "
    "Одинаковые числа не означают полную приёмку или совпадение ID."
)


class BadInputError(Exception):
    """Raised when input files or contents are invalid, corrupted, or violate preconditions."""


def check_snapshot_sidecars(snapshot_path: str) -> None:
    """Ensure WAL and rollback journal sidecar files do not exist or are empty (closed snapshot)."""
    wal_path = snapshot_path + "-wal"
    if os.path.exists(wal_path):
        try:
            if os.path.getsize(wal_path) > 0:
                raise BadInputError("Non-empty WAL file detected beside snapshot")
        except OSError:
            raise BadInputError("Cannot inspect WAL file beside snapshot")

    journal_path = snapshot_path + "-journal"
    if os.path.exists(journal_path):
        try:
            if os.path.getsize(journal_path) > 0:
                raise BadInputError("Non-empty journal file detected beside snapshot")
        except OSError:
            raise BadInputError("Cannot inspect journal file beside snapshot")


def read_snapshot(snapshot_path: str) -> tuple[dict[str, int], list[str]]:
    """Read and validate the SQLite snapshot database.

    Returns (counts_dict, blockers_list).
    Raises BadInputError on missing, empty, or schema-invalid database.
    """
    if not os.path.exists(snapshot_path):
        raise BadInputError("Snapshot file does not exist")
    if not os.path.isfile(snapshot_path):
        raise BadInputError("Snapshot path is not a regular file")
    try:
        if os.path.getsize(snapshot_path) == 0:
            raise BadInputError("Snapshot file is empty")
    except OSError:
        raise BadInputError("Cannot inspect snapshot file")

    abs_path = os.path.abspath(snapshot_path)
    check_snapshot_sidecars(abs_path)

    # Open with mode=ro&immutable=1 to strictly prevent file creation or modification
    file_url = pathname2url(abs_path)
    uri = f"file:{file_url}?mode=ro&immutable=1"

    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise BadInputError("Failed to open SQLite snapshot database in read-only immutable mode")

    try:
        cursor = conn.cursor()
        # Check required schema
        table_check = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if not table_check:
            raise BadInputError("Required table 'runs' is missing from snapshot database")

        col_rows = cursor.execute("PRAGMA table_info(runs)").fetchall()
        cols = {row[1] for row in col_rows}
        required_cols = {"run_id", "scenario", "status"}
        if not required_cols.issubset(cols):
            raise BadInputError("Required columns (run_id, scenario, status) missing from 'runs' table")

        rows = cursor.execute("SELECT run_id, scenario, status FROM runs").fetchall()
    except sqlite3.Error:
        raise BadInputError("Database error reading table 'runs'")
    finally:
        conn.close()

    db_counts: dict[str, int] = {st: 0 for st in TERMINAL_STATUSES}
    blockers: list[str] = []
    has_extra_scenarios = False
    has_pending = False
    has_unknown_status = False

    for _run_id, scenario, status in rows:
        if scenario != EXPECTED_SCENARIO:
            has_extra_scenarios = True
        if status in ("queued", "running"):
            has_pending = True
        elif status in TERMINAL_STATUSES:
            if scenario == EXPECTED_SCENARIO:
                db_counts[status] += 1
        else:
            has_unknown_status = True

    if has_extra_scenarios:
        blockers.append("Snapshot contains scenarios other than usb_diagnostics")
    if has_pending:
        blockers.append("Snapshot contains pending runs (queued or running)")
    if has_unknown_status:
        blockers.append("Snapshot contains runs with unrecognized or non-terminal status")

    return db_counts, blockers


def read_events(events_path: str) -> tuple[dict[str, int], list[str], dict[str, Any]]:
    """Read and validate the events JSONL stream.

    Returns (counts_dict, blockers_list, lifecycle_info).
    Raises BadInputError on empty, corrupted, non-monotonic, or schema-invalid lines.
    """
    if not os.path.exists(events_path):
        raise BadInputError("Events file does not exist")
    if not os.path.isfile(events_path):
        raise BadInputError("Events path is not a regular file")
    try:
        if os.path.getsize(events_path) == 0:
            raise BadInputError("Events file is empty")
    except OSError:
        raise BadInputError("Cannot inspect events file")

    jsonl_counts: dict[str, int] = {st: 0 for st in TERMINAL_STATUSES}
    has_start = False
    has_shutdown = False
    first_start_idx: Optional[int] = None
    last_shutdown_idx: Optional[int] = None
    event_count = 0
    last_elapsed: Optional[float] = None

    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    raise BadInputError(f"Invalid JSON at line {line_no}")

                if not isinstance(event, dict):
                    raise BadInputError(f"Event at line {line_no} must be a JSON object")

                if "run_id" in event:
                    raise BadInputError(f"Event at line {line_no} must not contain run_id")

                event_type = event.get("type")
                if event_type not in VALID_EVENT_TYPES:
                    raise BadInputError(f"Invalid event type at line {line_no}")

                if "elapsed_sec" not in event:
                    raise BadInputError(f"Missing elapsed_sec at line {line_no}")

                elapsed = event["elapsed_sec"]
                if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
                    raise BadInputError(f"Invalid elapsed_sec type at line {line_no}")
                if not math.isfinite(elapsed) or elapsed < 0:
                    raise BadInputError(f"Invalid elapsed_sec value at line {line_no}")

                if last_elapsed is not None and elapsed < last_elapsed:
                    raise BadInputError(f"Non-monotonic elapsed_sec at line {line_no}")
                last_elapsed = float(elapsed)

                if event_type == "start":
                    if first_start_idx is None:
                        first_start_idx = event_count
                    has_start = True
                elif event_type == "shutdown":
                    last_shutdown_idx = event_count
                    has_shutdown = True
                elif event_type == "run":
                    st = event.get("status")
                    if st not in TERMINAL_STATUSES:
                        raise BadInputError(f"Invalid or missing run status at line {line_no}")
                    jsonl_counts[st] += 1

                event_count += 1
    except UnicodeDecodeError:
        raise BadInputError("Events file is not valid UTF-8 text")
    except OSError:
        raise BadInputError("Cannot read events file")

    if event_count == 0:
        raise BadInputError("Events file contains no events")

    lifecycle_blockers: list[str] = []
    if not has_start:
        lifecycle_blockers.append("Missing start event")
    if not has_shutdown:
        lifecycle_blockers.append("Missing shutdown event")
    if has_start and has_shutdown:
        if first_start_idx is not None and last_shutdown_idx is not None:
            if first_start_idx > last_shutdown_idx:
                lifecycle_blockers.append("Shutdown event occurred before start event")
            if last_shutdown_idx != event_count - 1:
                lifecycle_blockers.append("Events recorded after shutdown event")

    lifecycle_info = {
        "has_start": has_start,
        "has_shutdown": has_shutdown,
        "complete": len(lifecycle_blockers) == 0,
        "event_count": event_count,
    }

    return jsonl_counts, lifecycle_blockers, lifecycle_info


def audit(events_path: str, snapshot_path: str) -> tuple[int, dict[str, Any]]:
    """Perform audit and return (exit_code, result_dict).

    Exit codes:
      0: match
      1: mismatch / incomplete
      2: bad input (raised via BadInputError in callers or returned if caught)
    """
    jsonl_counts, lifecycle_blockers, lifecycle_info = read_events(events_path)
    db_counts, db_blockers = read_snapshot(snapshot_path)

    count_mismatches: list[str] = []
    for st in TERMINAL_STATUSES:
        ev_c = jsonl_counts[st]
        db_c = db_counts[st]
        if ev_c != db_c:
            count_mismatches.append(f"Count mismatch for '{st}': events={ev_c}, snapshot={db_c}")

    all_differences = lifecycle_blockers + db_blockers + count_mismatches

    if not all_differences:
        status = "match"
        exit_code = 0
    elif lifecycle_blockers or "Snapshot contains pending runs (queued or running)" in db_blockers:
        status = "incomplete"
        exit_code = 1
    else:
        status = "mismatch"
        exit_code = 1

    result = {
        "status": status,
        "match": (exit_code == 0),
        "scenario": EXPECTED_SCENARIO,
        "lifecycle": {
            "has_start": lifecycle_info["has_start"],
            "has_shutdown": lifecycle_info["has_shutdown"],
            "complete": lifecycle_info["complete"],
        },
        "counts": {
            "events": {**jsonl_counts, "total": sum(jsonl_counts.values())},
            "snapshot": {**db_counts, "total": sum(db_counts.values())},
        },
        "differences": all_differences,
        "limitation": LIMITATION_NOTE,
    }

    return exit_code, result


def format_text_output(result: dict[str, Any]) -> str:
    """Format structured audit result as human-readable text."""
    lines = [
        "=== DroidHub USB Pilot Journal Audit ===",
        f"Status: {result['status'].upper()}",
        f"Scenario: {result['scenario']}",
        f"Lifecycle: {'COMPLETE' if result['lifecycle']['complete'] else 'INCOMPLETE'}",
        "Terminal Status Counts:",
    ]
    for st in TERMINAL_STATUSES:
        ev_c = result["counts"]["events"][st]
        db_c = result["counts"]["snapshot"][st]
        match_mark = "OK" if ev_c == db_c else "MISMATCH"
        lines.append(f"  {st:<10}: events={ev_c:<4} snapshot={db_c:<4} [{match_mark}]")

    lines.append(
        f"  {'total':<10}: events={result['counts']['events']['total']:<4} snapshot={result['counts']['snapshot']['total']:<4}"
    )

    if result["differences"]:
        lines.append("Differences / Blockers:")
        for diff in result["differences"]:
            lines.append(f"  - {diff}")

    lines.append("")
    lines.append(f"Limitation: {result['limitation']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for audit_desktop_pilot_journal."""
    parser = argparse.ArgumentParser(
        description="Audit isolated DroidHub desktop USB pilot artifacts."
    )
    parser.add_argument("--events", required=True, help="Path to events JSONL file")
    parser.add_argument("--snapshot", required=True, help="Path to SQLite snapshot database")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code

    try:
        exit_code, result = audit(args.events, args.snapshot)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_text_output(result))
        return exit_code
    except BadInputError as err:
        if args.json:
            err_result = {
                "status": "bad_input",
                "match": False,
                "error": str(err),
                "limitation": LIMITATION_NOTE,
            }
            print(json.dumps(err_result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(f"Error: {err}\n")
        return 2
    except Exception:
        if args.json:
            err_result = {
                "status": "bad_input",
                "match": False,
                "error": "Unexpected internal processing error",
                "limitation": LIMITATION_NOTE,
            }
            print(json.dumps(err_result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write("Error: Unexpected internal processing error\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
