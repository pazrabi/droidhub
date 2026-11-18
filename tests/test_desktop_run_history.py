"""Tests for desktop/run_history.py

Covers:
- list_runs: basic, limit, keyset cursor, identical timestamps, missing file,
  malformed file, invalid limits, path with spaces / URI-special chars.
- get_run: basic with targets, null result omission, missing run, missing file,
  malformed file, WAL mode.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from desktop.run_history import RunHistoryError, get_run, list_runs

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    scenario     TEXT,
    request_id   TEXT UNIQUE,
    params       TEXT,
    parent_run_id TEXT,
    status       TEXT,
    created_at   INTEGER
);
CREATE TABLE IF NOT EXISTS run_targets (
    run_id    TEXT,
    device_id TEXT,
    ord       INTEGER,
    status    TEXT,
    result    TEXT,
    PRIMARY KEY (run_id, device_id)
);
"""


def _make_db(path: str | Path, *, wal: bool = False) -> sqlite3.Connection:
    """Create (or open) a db at *path*, apply DDL, return the connection."""
    conn = sqlite3.connect(str(path))
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta VALUES ('schema_version','1')"
    )
    conn.commit()
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    scenario: str = "smoke",
    request_id: str | None = None,
    params: dict | None = None,
    parent_run_id: str | None = None,
    status: str = "done",
    created_at: int = 1_700_000_000_000,
) -> None:
    request_id = request_id or run_id
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            run_id,
            scenario,
            request_id,
            json.dumps(params) if params is not None else None,
            parent_run_id,
            status,
            created_at,
        ),
    )
    conn.commit()


def _insert_target(
    conn: sqlite3.Connection,
    run_id: str,
    device_id: str,
    ord: int,
    *,
    status: str = "done",
    result: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO run_targets VALUES (?,?,?,?,?)",
        (run_id, device_id, ord, status, json.dumps(result) if result is not None else None),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db_path(tmp_path):
    """Return a Path pointing to a fresh database file."""
    db = tmp_path / "history.db"
    conn = _make_db(db)
    conn.close()
    return db


@pytest.fixture()
def tmp_db_with_runs(tmp_path):
    """DB pre-populated with 5 runs in reverse chronological order."""
    db = tmp_path / "history.db"
    conn = _make_db(db)
    for i in range(5):
        _insert_run(
            conn,
            f"run-{i:03d}",
            scenario=f"scenario_{i}",
            status="done",
            created_at=1_700_000_000_000 + i * 1000,
        )
    conn.close()
    return db


@pytest.fixture()
def wal_db(tmp_path):
    """DB using WAL journal mode."""
    db = tmp_path / "wal.db"
    conn = _make_db(db, wal=True)
    _insert_run(conn, "wal-run-1", created_at=1_000_000_000)
    conn.close()
    return db


# ---------------------------------------------------------------------------
# list_runs: missing / malformed
# ---------------------------------------------------------------------------

def test_list_runs_missing_file(tmp_path):
    result = list_runs(tmp_path / "nonexistent.db")
    assert result == []


def test_list_runs_malformed_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"THIS IS NOT SQLITE")
    with pytest.raises(RunHistoryError):
        list_runs(bad)


# ---------------------------------------------------------------------------
# list_runs: basic queries
# ---------------------------------------------------------------------------

def test_list_runs_empty_db(tmp_db_path):
    result = list_runs(tmp_db_path)
    assert result == []


def test_list_runs_returns_newest_first(tmp_db_with_runs):
    rows = list_runs(tmp_db_with_runs)
    assert len(rows) == 5
    # created_at should be in descending order
    times = [r["created_at"] for r in rows]
    assert times == sorted(times, reverse=True)


def test_list_runs_row_keys(tmp_db_with_runs):
    rows = list_runs(tmp_db_with_runs)
    expected_keys = {"run_id", "scenario", "status", "created_at", "parent_run_id", "target_count"}
    assert set(rows[0].keys()) == expected_keys


def test_list_runs_target_count(tmp_path):
    db = tmp_path / "tc.db"
    conn = _make_db(db)
    _insert_run(conn, "r1")
    _insert_target(conn, "r1", "dev-a", 0)
    _insert_target(conn, "r1", "dev-b", 1)
    conn.close()

    rows = list_runs(db)
    assert len(rows) == 1
    assert rows[0]["target_count"] == 2


def test_list_runs_no_targets_count_zero(tmp_db_with_runs):
    rows = list_runs(tmp_db_with_runs)
    for r in rows:
        assert r["target_count"] == 0


# ---------------------------------------------------------------------------
# list_runs: limit
# ---------------------------------------------------------------------------

def test_list_runs_default_limit_fifty(tmp_path):
    db = tmp_path / "big.db"
    conn = _make_db(db)
    for i in range(60):
        _insert_run(conn, f"r{i:04d}", created_at=1_700_000_000_000 + i)
    conn.close()

    rows = list_runs(db)
    assert len(rows) == 50


def test_list_runs_custom_limit(tmp_db_with_runs):
    rows = list_runs(tmp_db_with_runs, limit=2)
    assert len(rows) == 2


def test_list_runs_limit_1(tmp_db_with_runs):
    rows = list_runs(tmp_db_with_runs, limit=1)
    assert len(rows) == 1


def test_list_runs_limit_100_accepted(tmp_path):
    db = tmp_path / "h.db"
    conn = _make_db(db)
    for i in range(100):
        _insert_run(conn, f"r{i:04d}", created_at=1_700_000_000_000 + i)
    conn.close()
    rows = list_runs(db, limit=100)
    assert len(rows) == 100


@pytest.mark.parametrize("bad_limit", [0, 101, -1, 1.5, "10", None])
def test_list_runs_invalid_limit(tmp_db_path, bad_limit):
    with pytest.raises((ValueError, TypeError)):
        list_runs(tmp_db_path, limit=bad_limit)


# ---------------------------------------------------------------------------
# list_runs: keyset cursor (before=)
# ---------------------------------------------------------------------------

def test_list_runs_cursor_pages_correctly(tmp_path):
    db = tmp_path / "cursor.db"
    conn = _make_db(db)
    for i in range(10):
        _insert_run(conn, f"r{i:04d}", created_at=1_700_000_000_000 + i * 1000)
    conn.close()

    page1 = list_runs(db, limit=4)
    assert len(page1) == 4

    last = page1[-1]
    cursor = (last["created_at"], last["run_id"])

    page2 = list_runs(db, limit=4, before=cursor)
    assert len(page2) == 4

    # No overlap
    ids1 = {r["run_id"] for r in page1}
    ids2 = {r["run_id"] for r in page2}
    assert ids1.isdisjoint(ids2)

    # page2 rows are older than page1 rows
    assert max(r["created_at"] for r in page2) < min(r["created_at"] for r in page1)


def test_list_runs_cursor_beyond_end(tmp_db_with_runs):
    rows = list_runs(tmp_db_with_runs)
    oldest = rows[-1]
    cursor = (oldest["created_at"], oldest["run_id"])
    result = list_runs(tmp_db_with_runs, before=cursor)
    assert result == []


def test_list_runs_identical_timestamps_stable_order(tmp_path):
    """Rows with equal created_at are ordered by run_id DESC for stability."""
    db = tmp_path / "same_ts.db"
    conn = _make_db(db)
    same_ts = 1_700_000_000_000
    # Insert in alphabetical order; we expect reverse-alphabetical from query
    for rid in ["aaa", "bbb", "ccc", "ddd"]:
        _insert_run(conn, rid, created_at=same_ts)
    conn.close()

    page1 = list_runs(db, limit=2)
    ids1 = [r["run_id"] for r in page1]
    # First page: descending run_id = ddd, ccc
    assert ids1 == ["ddd", "ccc"]

    cursor = (page1[-1]["created_at"], page1[-1]["run_id"])
    page2 = list_runs(db, limit=2, before=cursor)
    ids2 = [r["run_id"] for r in page2]
    # Second page: bbb, aaa
    assert ids2 == ["bbb", "aaa"]

    # No overlap, full coverage
    assert set(ids1) | set(ids2) == {"aaa", "bbb", "ccc", "ddd"}
    assert set(ids1).isdisjoint(set(ids2))


# ---------------------------------------------------------------------------
# list_runs: path with spaces / URI-special characters
# ---------------------------------------------------------------------------

def test_list_runs_path_with_spaces(tmp_path):
    spaced = tmp_path / "my history db"
    spaced.mkdir()
    db = spaced / "runs.db"
    conn = _make_db(db)
    _insert_run(conn, "run-space-1")
    conn.close()

    rows = list_runs(db)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-space-1"


def test_list_runs_path_with_percent(tmp_path):
    pct = tmp_path / "data 100%"
    pct.mkdir()
    db = pct / "runs.db"
    conn = _make_db(db)
    _insert_run(conn, "run-pct-1")
    conn.close()

    rows = list_runs(db)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-pct-1"


# ---------------------------------------------------------------------------
# get_run: missing / malformed
# ---------------------------------------------------------------------------

def test_get_run_missing_file(tmp_path):
    result = get_run(tmp_path / "nope.db", "any-id")
    assert result is None


def test_get_run_malformed_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"GARBAGE DATA")
    with pytest.raises(RunHistoryError):
        get_run(bad, "any-id")


def test_get_run_unknown_run_id(tmp_db_path):
    assert get_run(tmp_db_path, "not-a-real-run") is None


# ---------------------------------------------------------------------------
# get_run: correct data
# ---------------------------------------------------------------------------

def test_get_run_basic(tmp_path):
    db = tmp_path / "basic.db"
    conn = _make_db(db)
    _insert_run(
        conn,
        "run-abc",
        scenario="full_smoke",
        request_id="req-001",
        params={"foo": "bar"},
        status="done",
        created_at=1_700_000_000_000,
    )
    conn.close()

    result = get_run(db, "run-abc")
    assert result is not None
    assert result["schema_version"] == 1
    assert result["run_id"] == "run-abc"
    assert result["scenario"] == "full_smoke"
    assert result["request_id"] == "req-001"
    assert result["params"] == {"foo": "bar"}
    assert result["parent_run_id"] is None
    assert result["status"] == "done"
    assert result["created_at"] == 1_700_000_000_000
    assert result["targets"] == []


def test_get_run_with_targets(tmp_path):
    db = tmp_path / "targets.db"
    conn = _make_db(db)
    _insert_run(conn, "run-t1")
    _insert_target(conn, "run-t1", "dev-0", 0, status="done", result={"ok": True})
    _insert_target(conn, "run-t1", "dev-1", 1, status="failed", result=None)
    conn.close()

    result = get_run(db, "run-t1")
    assert result is not None
    assert len(result["targets"]) == 2

    t0 = result["targets"][0]
    assert t0["device_id"] == "dev-0"
    assert t0["status"] == "done"
    assert t0["result"] == {"ok": True}

    t1 = result["targets"][1]
    assert t1["device_id"] == "dev-1"
    assert t1["status"] == "failed"
    assert "result" not in t1  # null result is omitted


def test_get_run_targets_ordered_by_ord(tmp_path):
    db = tmp_path / "ord.db"
    conn = _make_db(db)
    _insert_run(conn, "run-ord")
    # Insert in reverse ord order to ensure ORDER BY works
    _insert_target(conn, "run-ord", "dev-b", 2, status="done")
    _insert_target(conn, "run-ord", "dev-a", 1, status="done")
    _insert_target(conn, "run-ord", "dev-c", 3, status="done")
    _insert_target(conn, "run-ord", "dev-z", 0, status="done")
    conn.close()

    result = get_run(db, "run-ord")
    device_ids = [t["device_id"] for t in result["targets"]]
    assert device_ids == ["dev-z", "dev-a", "dev-b", "dev-c"]


def test_get_run_params_null(tmp_path):
    db = tmp_path / "null_params.db"
    conn = _make_db(db)
    _insert_run(conn, "run-np", params=None)
    conn.close()

    result = get_run(db, "run-np")
    assert result["params"] is None


def test_get_run_preserves_queued_running_status(tmp_path):
    """Reader must not normalise live statuses like queued/running."""
    db = tmp_path / "live.db"
    conn = _make_db(db)
    _insert_run(conn, "run-q", status="queued")
    _insert_run(conn, "run-r", status="running", created_at=1_700_000_001_000)
    conn.close()

    q = get_run(db, "run-q")
    r = get_run(db, "run-r")
    assert q["status"] == "queued"
    assert r["status"] == "running"


def test_get_run_parent_run_id(tmp_path):
    db = tmp_path / "parent.db"
    conn = _make_db(db)
    _insert_run(conn, "parent-run")
    _insert_run(conn, "child-run", parent_run_id="parent-run", created_at=1_700_000_001_000)
    conn.close()

    child = get_run(db, "child-run")
    assert child["parent_run_id"] == "parent-run"


# ---------------------------------------------------------------------------
# WAL mode
# ---------------------------------------------------------------------------

def test_list_runs_wal_mode(wal_db):
    rows = list_runs(wal_db)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "wal-run-1"


def test_get_run_wal_mode(wal_db):
    result = get_run(wal_db, "wal-run-1")
    assert result is not None
    assert result["run_id"] == "wal-run-1"
