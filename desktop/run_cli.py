"""
desktop.run_cli — DroidHub Desktop M1 CLI
==========================================

Usage:
  python -m desktop.run_cli [--database PATH] [--json] <command> ...

Commands:
  history   List recent check runs.
  status    Show details for a single run.
  check     Run a readiness check on one or more devices.

Global options:
  --database PATH   Path to the SQLite database file.
                    Defaults to <storage_dir>/script-runs.sqlite3.
  --json            Output results as JSON (schema_version=1).
                    JSON is printed to stdout; diagnostics go to stderr.

Exit codes:
  0   Success (or completed report for partial/failed/cancelled runs).
  1   Bad arguments / validation error.
  2   Run not found (status command).
  3   Workflow/executor error (check command).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Lazy dependency accessors
# These are deferred so the CLI can be imported in tests without installing
# the full production stack.
# ---------------------------------------------------------------------------

def _get_default_db_path() -> str:  # pragma: no cover
    """Return the default database path (lazily imports desktop.link_storage)."""
    from desktop.link_storage import get_default_storage_dir  # type: ignore[import]
    import os
    return os.path.join(get_default_storage_dir(), "script-runs.sqlite3")


def _default_list_runs(
    path: str,
    *,
    limit: int = 50,
    before: Any = None,
) -> list[dict]:  # pragma: no cover
    from desktop.run_history import list_runs  # type: ignore[import]
    return list_runs(path, limit=limit, before=before)


def _default_get_run(path: str, run_id: str) -> dict | None:  # pragma: no cover
    from desktop.run_history import get_run  # type: ignore[import]
    return get_run(path, run_id)


def _default_run_check(
    path: str,
    serials: list[str],
    *,
    min_battery: int,
) -> dict:  # pragma: no cover
    from desktop.readiness_runtime import run_check  # type: ignore[import]
    return run_check(path, serials, min_battery=min_battery)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit_json(data: dict) -> None:
    """Write JSON (with schema_version=1 guaranteed) to stdout."""
    out: dict = {"schema_version": 1}
    out.update(data)
    print(json.dumps(out, ensure_ascii=False))


def _emit_text(text: str) -> None:
    print(text)


def _emit_error(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _emit_diag(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m desktop.run_cli",
        description=(
            "DroidHub Desktop M1 CLI — manage and inspect device readiness checks.\n\n"
            "All subcommands accept --json (machine-readable output) and\n"
            "--database (custom SQLite path). JSON output always includes\n"
            "schema_version=1. Diagnostics are written to stderr."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database",
        metavar="PATH",
        default=None,
        help=(
            "Path to the script-runs SQLite database. "
            "Defaults to <storage_dir>/script-runs.sqlite3."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON to stdout (schema_version=1).",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # --- history ---
    p_hist = sub.add_parser(
        "history",
        help="List recent check runs.",
        description=(
            "List recent check runs stored in the database.\n\n"
            "Exit codes:\n"
            "  0  Success.\n"
            "  1  Bad arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_hist.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Maximum number of runs to return (default: 50, minimum: 1).",
    )

    # --- status ---
    p_stat = sub.add_parser(
        "status",
        help="Show details for a single run by its RUN_ID.",
        description=(
            "Display detailed information for a single run.\n\n"
            "Exit codes:\n"
            "  0  Run found.\n"
            "  1  Bad arguments.\n"
            "  2  Run not found."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_stat.add_argument(
        "run_id",
        metavar="RUN_ID",
        help="Identifier of the run to look up.",
    )

    # --- check ---
    p_chk = sub.add_parser(
        "check",
        help="Run a readiness check on one or more devices.",
        description=(
            "Run a readiness check on the specified device(s).\n\n"
            "Rules:\n"
            "  * --device may be repeated; at least one is required.\n"
            "  * No duplicate serial numbers are allowed.\n"
            "  * --min-battery must be between 0 and 100 (inclusive).\n"
            "  * --confirm is required; it must list the exact set of serials\n"
            "    provided via --device (order does not matter).\n"
            "  * The CLI never writes to the database directly; it delegates\n"
            "    to desktop.readiness_runtime.run_check.\n\n"
            "Returns the full persisted report (schema_version=1) even when\n"
            "the check status is partial, failed, or cancelled.\n\n"
            "Exit codes:\n"
            "  0  Executor returned a report (any terminal status).\n"
            "  1  Bad arguments or confirmation mismatch.\n"
            "  3  Executor raised an exception."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_chk.add_argument(
        "--device",
        action="append",
        dest="devices",
        metavar="SERIAL",
        required=True,
        help="Device serial (repeatable, at least one required).",
    )
    p_chk.add_argument(
        "--min-battery",
        type=int,
        default=20,
        metavar="N",
        help="Minimum acceptable battery level 0–100 (default: 20).",
    )
    p_chk.add_argument(
        "--confirm",
        nargs="+",
        metavar="SERIAL",
        required=True,
        help=(
            "Explicit confirmation of the target device serials. "
            "Must match exactly the set provided via --device."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_history(
    args: argparse.Namespace,
    db_path: str,
    *,
    list_runs_fn: Callable,
) -> int:
    if args.limit < 1:
        _emit_error(f"--limit must be at least 1, got {args.limit}")
        return 1

    runs = list_runs_fn(db_path, limit=args.limit)

    if args.json:
        _emit_json({"runs": runs})
    else:
        if not runs:
            _emit_text("No runs found.")
        else:
            for r in runs:
                _emit_text(
                    f"{r.get('run_id', '?')}  "
                    f"{r.get('status', '?'):<12}  "
                    f"{r.get('started_at', '')}"
                )
    return 0


def _cmd_status(
    args: argparse.Namespace,
    db_path: str,
    *,
    get_run_fn: Callable,
) -> int:
    run = get_run_fn(db_path, args.run_id)
    if run is None:
        _emit_error(f"run not found: {args.run_id}")
        return 2

    if args.json:
        _emit_json(run)
    else:
        for key, value in run.items():
            _emit_text(f"{key}: {value}")
    return 0


def _cmd_check(
    args: argparse.Namespace,
    db_path: str,
    *,
    run_check_fn: Callable,
) -> int:
    # --- Validate devices ---
    devices: list[str] = args.devices or []
    if not devices:
        _emit_error("at least one --device SERIAL is required")
        return 1
    for serial in devices:
        if not serial or not serial.strip():
            _emit_error("device serial must not be empty")
            return 1
    if len(devices) != len(set(devices)):
        _emit_error("duplicate device serials are not allowed")
        return 1

    # --- Validate battery threshold ---
    if not (0 <= args.min_battery <= 100):
        _emit_error(f"--min-battery must be between 0 and 100, got {args.min_battery}")
        return 1

    # --- Validate confirmation ---
    confirm: list[str] = args.confirm or []
    if set(confirm) != set(devices):
        expected = sorted(devices)
        got = sorted(confirm)
        _emit_error(
            f"--confirm must list exactly the same serials as --device.\n"
            f"  Expected: {expected}\n"
            f"  Got:      {got}"
        )
        return 1
    if len(confirm) != len(set(confirm)):
        _emit_error("duplicate serials in --confirm are not allowed")
        return 1

    # --- Execute ---
    _emit_diag(f"Running readiness check on {len(devices)} device(s): {devices}")
    try:
        report: dict = run_check_fn(db_path, devices, min_battery=args.min_battery)
    except Exception as exc:  # noqa: BLE001
        _emit_error(f"executor error: {exc}")
        return 3

    if args.json:
        _emit_json(report)
    else:
        _emit_text(f"run_id:  {report.get('run_id', '?')}")
        _emit_text(f"status:  {report.get('status', '?')}")
        targets = report.get("targets", [])
        if targets:
            _emit_text("targets:")
            for t in targets:
                _emit_text(
                    f"  {t.get('serial', '?')}: {t.get('status', '?')}"
                    + (f"  battery={t.get('battery')}%" if t.get("battery") is not None else "")
                )
    return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(
    argv: list[str] | None = None,
    *,
    db_path_fn: Callable[[], str] | None = None,
    list_runs_fn: Callable | None = None,
    get_run_fn: Callable | None = None,
    run_check_fn: Callable | None = None,
) -> int:
    """
    Parse *argv* (defaults to sys.argv[1:]) and dispatch to the appropriate
    handler.

    Keyword-only callables allow tests to inject fakes without touching the
    production import path.  Pass None (the default) to use the real
    production implementations.

    Returns an integer exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve database path
    if args.database:
        db_path = args.database
    else:
        if db_path_fn is not None:
            db_path = db_path_fn()
        else:
            db_path = _get_default_db_path()  # pragma: no cover

    # Resolve callables
    _list_runs = list_runs_fn if list_runs_fn is not None else _default_list_runs
    _get_run = get_run_fn if get_run_fn is not None else _default_get_run
    _run_check = run_check_fn if run_check_fn is not None else _default_run_check

    if args.command == "history":
        return _cmd_history(args, db_path, list_runs_fn=_list_runs)
    if args.command == "status":
        return _cmd_status(args, db_path, get_run_fn=_get_run)
    if args.command == "check":
        return _cmd_check(args, db_path, run_check_fn=_run_check)

    # Should be unreachable due to sub.required = True
    _emit_error(f"unknown command: {args.command}")  # pragma: no cover
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
