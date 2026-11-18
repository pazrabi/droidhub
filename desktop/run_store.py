"""
SQLite-backed journal for DroidHub Desktop run tracking.

Schema version: 1
"""

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

SCHEMA_VERSION = 1

# Allowed keys for params
_ALLOWED_PARAMS = frozenset({"min_battery", "parallel", "route"})

# Allowed keys for target result
_ALLOWED_RESULT_FIELDS = frozenset({
    "status", "step", "code", "last_ok_step",
    "safe_retry", "route", "steps", "battery", "device_id",
})

# Terminal target statuses
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

_DDL = """\
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    scenario      TEXT NOT NULL,
    request_id    TEXT NOT NULL UNIQUE,
    params        TEXT NOT NULL,   -- JSON
    parent_run_id TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_targets (
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    device_id TEXT NOT NULL,
    ord       INTEGER NOT NULL,   -- insertion order
    status    TEXT NOT NULL DEFAULT 'queued',
    result    TEXT,               -- JSON snapshot when terminal
    PRIMARY KEY (run_id, device_id)
);
"""


class RunStore:
    """Persistent SQLite journal for desktop run tracking."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_DDL)
            # Use INSERT OR IGNORE so two concurrent instances don't collide on
            # the PRIMARY KEY and also don't insert duplicate metadata rows.
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )
            row = cur.fetchone()
            stored = int(row["value"])
            if stored != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported schema version {stored}; expected {SCHEMA_VERSION}"
                )

    @staticmethod
    def _validate_params(params: dict) -> None:
        unknown = set(params) - _ALLOWED_PARAMS
        if unknown:
            raise ValueError(f"Unknown param keys: {sorted(unknown)}")

    @staticmethod
    def _validate_result(result: dict) -> None:
        unknown = set(result) - _ALLOWED_RESULT_FIELDS
        if unknown:
            raise ValueError(f"Unknown result keys: {sorted(unknown)}")

    @staticmethod
    def _compute_run_status(target_rows) -> str:
        """Derive aggregate run status from target rows.

        Rules:
        - All queued                              → queued
        - All terminal, all succeeded             → succeeded
        - All terminal, all failed                → failed
        - All terminal, all cancelled             → cancelled
        - All terminal, some succeeded, rest not  → partial
        - All terminal, none succeeded            → failed
        - Some terminal, some still queued        → running
        """
        statuses = [r["status"] for r in target_rows]
        all_terminal = all(s in _TERMINAL_STATUSES for s in statuses)
        any_terminal = any(s in _TERMINAL_STATUSES for s in statuses)

        if not any_terminal:
            return "queued"

        if not all_terminal:
            # At least one still queued (or non-terminal) → running
            return "running"

        # All terminal from here
        any_succeeded = any(s == "succeeded" for s in statuses)
        all_succeeded = all(s == "succeeded" for s in statuses)
        all_failed = all(s == "failed" for s in statuses)
        all_cancelled = all(s == "cancelled" for s in statuses)

        if all_succeeded:
            return "succeeded"
        if all_failed:
            return "failed"
        if all_cancelled:
            return "cancelled"
        if any_succeeded:
            return "partial"
        return "failed"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        scenario: str,
        request_id: str,
        targets: list[str],
        params: dict[str, Any],
        parent_run_id: str | None = None,
    ) -> str:
        """Create a new run or return an existing run for the same request_id.

        If request_id already exists, the call is a no-op and returns the
        existing run_id — UNLESS the snapshot differs, which raises ValueError.
        """
        if not targets:
            raise ValueError("targets must be a non-empty list")
        if len(targets) != len(set(targets)):
            raise ValueError("targets must be unique")
        self._validate_params(params)

        params_json = json.dumps(params, sort_keys=True)

        with self._lock:
            # BEGIN IMMEDIATE acquires a write lock up front, so two concurrent
            # callers with the same request_id are serialized: exactly one will
            # see row=None and INSERT; the other will find the existing row.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT run_id, scenario, params, parent_run_id FROM runs WHERE request_id=?",
                    (request_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    # Validate snapshot matches
                    if (
                        existing["scenario"] != scenario
                        or existing["params"] != params_json
                        or existing["parent_run_id"] != parent_run_id
                    ):
                        raise ValueError(
                            f"request_id {request_id!r} already exists with a different snapshot"
                        )
                    # Also check targets match
                    tcur = self._conn.execute(
                        "SELECT device_id FROM run_targets WHERE run_id=? ORDER BY ord",
                        (existing["run_id"],),
                    )
                    existing_targets = [r["device_id"] for r in tcur.fetchall()]
                    if existing_targets != targets:
                        raise ValueError(
                            f"request_id {request_id!r} already exists with different targets"
                        )
                    self._conn.execute("COMMIT")
                    return existing["run_id"]

                # New run
                run_id = str(uuid.uuid4())
                self._conn.execute(
                    "INSERT INTO runs(run_id,scenario,request_id,params,parent_run_id,status,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (run_id, scenario, request_id, params_json, parent_run_id, "queued",
                     int(time.time() * 1000)),
                )
                for ord_, device_id in enumerate(targets):
                    self._conn.execute(
                        "INSERT INTO run_targets(run_id,device_id,ord,status) VALUES(?,?,?,?)",
                        (run_id, device_id, ord_, "queued"),
                    )
                self._conn.execute("COMMIT")
                return run_id
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def record_target(self, run_id: str, device_id: str, result: dict[str, Any]) -> None:
        """Record a terminal result for one target device.

        Identical replays are a no-op. A conflicting update raises ValueError.
        Unknown result keys raise ValueError.
        If result contains device_id and it differs from the target, raises ValueError.
        """
        self._validate_result(result)

        status = result.get("status")
        if not isinstance(status, str) or status not in _TERMINAL_STATUSES:
            raise ValueError(
                f"result.status must be one of {sorted(_TERMINAL_STATUSES)}, got {status!r}"
            )

        # Reject mismatched device_id in result payload
        if "device_id" in result and result["device_id"] != device_id:
            raise ValueError(
                f"result.device_id {result['device_id']!r} does not match target device_id {device_id!r}"
            )

        result_json = json.dumps(result, sort_keys=True)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT status, result FROM run_targets WHERE run_id=? AND device_id=?",
                    (run_id, device_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(
                        f"device_id {device_id!r} is not a target of run {run_id!r}"
                    )

                existing_status = row["status"]
                existing_result = row["result"]

                if existing_status in _TERMINAL_STATUSES:
                    # Already terminal — allow identical replay, reject conflict
                    if existing_result == result_json:
                        self._conn.execute("COMMIT")
                        return  # no-op
                    raise ValueError(
                        f"target {device_id!r} already has a terminal result that differs"
                    )

                self._conn.execute(
                    "UPDATE run_targets SET status=?, result=? WHERE run_id=? AND device_id=?",
                    (status, result_json, run_id, device_id),
                )

                # Recompute aggregate run status
                tcur = self._conn.execute(
                    "SELECT status FROM run_targets WHERE run_id=?",
                    (run_id,),
                )
                all_rows = tcur.fetchall()
                new_run_status = self._compute_run_status(all_rows)
                self._conn.execute(
                    "UPDATE runs SET status=? WHERE run_id=?",
                    (new_run_status, run_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return the full run snapshot or None if not found."""
        with self._lock:
            # Use a deferred transaction for a consistent read snapshot
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "SELECT run_id, scenario, request_id, params, parent_run_id, status"
                    " FROM runs WHERE run_id=?",
                    (run_id,),
                )
                run_row = cur.fetchone()
                if run_row is None:
                    self._conn.execute("COMMIT")
                    return None

                tcur = self._conn.execute(
                    "SELECT device_id, status, result FROM run_targets"
                    " WHERE run_id=? ORDER BY ord",
                    (run_id,),
                )
                target_rows = tcur.fetchall()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        targets = []
        for tr in target_rows:
            t: dict[str, Any] = {
                "device_id": tr["device_id"],
                "status": tr["status"],
            }
            if tr["result"] is not None:
                t["result"] = json.loads(tr["result"])
            targets.append(t)

        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_row["run_id"],
            "scenario": run_row["scenario"],
            "request_id": run_row["request_id"],
            "params": json.loads(run_row["params"]),
            "parent_run_id": run_row["parent_run_id"],
            "status": run_row["status"],
            "targets": targets,
        }

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
