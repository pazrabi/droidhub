"""
DroidHub Desktop – read-only run history access layer.

Uses the stdlib sqlite3 module with URI mode (mode=ro) so the file is never
modified.  A live WAL database is handled correctly because we do NOT pass
immutable=1; SQLite will still return committed data consistently within the
BEGIN DEFERRED transaction we open here.

Public API
----------
list_runs(path, *, limit=50, before=None)  -> list[dict]
get_run(path, run_id)                      -> dict | None

Raises
------
RunHistoryError  – file exists but is malformed or inaccessible.
Missing file     – returns [] / None silently (desktop hasn't written yet).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Optional

__all__ = ["RunHistoryError", "list_runs", "get_run"]


class RunHistoryError(Exception):
    """Raised when the database file exists but cannot be read correctly."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db_uri(path: str | Path) -> str:
    """Convert a filesystem path to a sqlite3 URI with mode=ro.

    Spaces, percent signs, and other URI-special characters in the path are
    percent-encoded so that sqlite3 interprets the path correctly.
    """
    # urllib.parse.quote encodes everything except the chars that are safe
    # inside a URI path.  '?' and '#' would break the query-string we append,
    # so they are not in the safe set.
    encoded = urllib.parse.quote(str(Path(path).resolve()), safe="/")
    return f"file:{encoded}?mode=ro"


def _connect_ro(path: str | Path) -> sqlite3.Connection:
    """Return a read-only connection or raise RunHistoryError."""
    uri = _db_uri(path)
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Verify it really is a SQLite database (catches corrupt / wrong files).
        conn.execute("SELECT 1")
        return conn
    except sqlite3.OperationalError as exc:
        raise RunHistoryError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise RunHistoryError(str(exc)) from exc


def _decode_json(value: Optional[str]) -> object:
    """Decode a JSON column value; None stays None."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_runs(
    path: str | Path,
    *,
    limit: int = 50,
    before: tuple[int, str] | None = None,
) -> list[dict]:
    """Return up to *limit* run rows, newest first.

    Parameters
    ----------
    path:
        Filesystem path to the SQLite database file.
    limit:
        Number of rows to return; must be between 1 and 100 inclusive.
    before:
        Exclusive keyset cursor expressed as ``(created_at, run_id)`` where
        ``created_at`` is epoch-milliseconds.  When supplied, only rows that
        come *after* this key in descending order are returned, enabling
        stable pagination even when new rows are inserted between pages.

    Returns
    -------
    list of dicts with keys: run_id, scenario, status, created_at,
    parent_run_id, target_count.

    Missing file returns [].  Existing malformed file raises RunHistoryError.
    """
    if not isinstance(limit, int) or not (1 <= limit <= 100):
        raise ValueError(f"limit must be an integer between 1 and 100, got {limit!r}")

    p = Path(path)
    if not p.exists():
        return []

    conn = _connect_ro(p)
    try:
        with conn:
            if before is None:
                rows = conn.execute(
                    """
                    SELECT
                        r.run_id,
                        r.scenario,
                        r.status,
                        r.created_at,
                        r.parent_run_id,
                        COUNT(rt.device_id) AS target_count
                    FROM runs r
                    LEFT JOIN run_targets rt ON rt.run_id = r.run_id
                    GROUP BY r.run_id
                    ORDER BY r.created_at DESC, r.run_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                before_ts, before_id = before
                rows = conn.execute(
                    """
                    SELECT
                        r.run_id,
                        r.scenario,
                        r.status,
                        r.created_at,
                        r.parent_run_id,
                        COUNT(rt.device_id) AS target_count
                    FROM runs r
                    LEFT JOIN run_targets rt ON rt.run_id = r.run_id
                    GROUP BY r.run_id
                    HAVING (r.created_at < ? OR (r.created_at = ? AND r.run_id < ?))
                    ORDER BY r.created_at DESC, r.run_id DESC
                    LIMIT ?
                    """,
                    (before_ts, before_ts, before_id, limit),
                ).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        raise RunHistoryError(str(exc)) from exc
    finally:
        conn.close()

    return [
        {
            "run_id": row["run_id"],
            "scenario": row["scenario"],
            "status": row["status"],
            "created_at": row["created_at"],
            "parent_run_id": row["parent_run_id"],
            "target_count": row["target_count"],
        }
        for row in rows
    ]


def get_run(path: str | Path, run_id: str) -> dict | None:
    """Return full details for a single run.

    Parameters
    ----------
    path:
        Filesystem path to the SQLite database file.
    run_id:
        The run to look up.

    Returns
    -------
    dict with keys: schema_version, run_id, scenario, request_id, params,
    parent_run_id, status, created_at, targets.

    ``targets`` is a list ordered by ``ord``, each entry has device_id,
    status, and (if non-null) result.

    Missing file returns None.  Existing malformed file raises RunHistoryError.
    """
    p = Path(path)
    if not p.exists():
        return None

    conn = _connect_ro(p)
    try:
        with conn:
            run_row = conn.execute(
                """
                SELECT run_id, scenario, request_id, params,
                       parent_run_id, status, created_at
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

            if run_row is None:
                return None

            target_rows = conn.execute(
                """
                SELECT device_id, ord, status, result
                FROM run_targets
                WHERE run_id = ?
                ORDER BY ord
                """,
                (run_id,),
            ).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        raise RunHistoryError(str(exc)) from exc
    finally:
        conn.close()

    targets = []
    for t in target_rows:
        entry: dict = {
            "device_id": t["device_id"],
            "status": t["status"],
        }
        result = _decode_json(t["result"])
        if result is not None:
            entry["result"] = result
        targets.append(entry)

    return {
        "schema_version": 1,
        "run_id": run_row["run_id"],
        "scenario": run_row["scenario"],
        "request_id": run_row["request_id"],
        "params": _decode_json(run_row["params"]),
        "parent_run_id": run_row["parent_run_id"],
        "status": run_row["status"],
        "created_at": run_row["created_at"],
        "targets": targets,
    }
