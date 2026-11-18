"""Tests for desktop/run_store.py"""
import json
import os
import tempfile

import pytest

from desktop.run_store import RunStore, SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    db = tmp_path / "runs.db"
    with RunStore(str(db)) as s:
        yield s


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "runs.db")


# ---------------------------------------------------------------------------
# create_run basics
# ---------------------------------------------------------------------------

class TestCreateRun:
    def test_returns_string_run_id(self, store):
        rid = store.create_run(
            scenario="smoke",
            request_id="req-1",
            targets=["dev-a"],
            params={},
        )
        assert isinstance(rid, str) and rid

    def test_get_run_shape(self, store):
        rid = store.create_run(
            scenario="smoke",
            request_id="req-2",
            targets=["dev-a", "dev-b"],
            params={"parallel": 2},
        )
        run = store.get_run(rid)
        assert run is not None
        assert run["schema_version"] == SCHEMA_VERSION
        assert run["run_id"] == rid
        assert run["scenario"] == "smoke"
        assert run["request_id"] == "req-2"
        assert run["params"] == {"parallel": 2}
        assert run["parent_run_id"] is None
        assert run["status"] == "queued"
        assert [t["device_id"] for t in run["targets"]] == ["dev-a", "dev-b"]
        assert all(t["status"] == "queued" for t in run["targets"])

    def test_parent_run_id_stored(self, store):
        rid = store.create_run(
            scenario="s",
            request_id="req-p",
            targets=["d1"],
            params={},
            parent_run_id="parent-xyz",
        )
        run = store.get_run(rid)
        assert run["parent_run_id"] == "parent-xyz"

    def test_all_params_accepted(self, store):
        rid = store.create_run(
            scenario="s",
            request_id="req-params",
            targets=["d1"],
            params={"min_battery": 50, "parallel": 1, "route": "north"},
        )
        run = store.get_run(rid)
        assert run["params"]["min_battery"] == 50

    def test_unknown_param_raises(self, store):
        with pytest.raises(ValueError, match="Unknown param keys"):
            store.create_run(
                scenario="s",
                request_id="req-unk",
                targets=["d1"],
                params={"turbo": True},
            )

    def test_empty_targets_raises(self, store):
        with pytest.raises(ValueError):
            store.create_run(scenario="s", request_id="r", targets=[], params={})

    def test_duplicate_targets_raises(self, store):
        with pytest.raises(ValueError):
            store.create_run(
                scenario="s", request_id="r", targets=["d1", "d1"], params={}
            )

    def test_get_run_none_for_missing(self, store):
        assert store.get_run("no-such-id") is None


# ---------------------------------------------------------------------------
# Idempotent dedup / conflict
# ---------------------------------------------------------------------------

class TestCreateRunDedup:
    def test_same_request_id_idempotent(self, store):
        rid1 = store.create_run(
            scenario="smoke",
            request_id="req-idem",
            targets=["d1", "d2"],
            params={"parallel": 2},
        )
        rid2 = store.create_run(
            scenario="smoke",
            request_id="req-idem",
            targets=["d1", "d2"],
            params={"parallel": 2},
        )
        assert rid1 == rid2

    def test_different_scenario_raises(self, store):
        store.create_run(
            scenario="smoke",
            request_id="req-conflict",
            targets=["d1"],
            params={},
        )
        with pytest.raises(ValueError, match="different snapshot"):
            store.create_run(
                scenario="full",
                request_id="req-conflict",
                targets=["d1"],
                params={},
            )

    def test_different_params_raises(self, store):
        store.create_run(
            scenario="s",
            request_id="req-cp",
            targets=["d1"],
            params={"parallel": 1},
        )
        with pytest.raises(ValueError, match="different snapshot"):
            store.create_run(
                scenario="s",
                request_id="req-cp",
                targets=["d1"],
                params={"parallel": 2},
            )

    def test_different_targets_raises(self, store):
        store.create_run(
            scenario="s",
            request_id="req-ct",
            targets=["d1"],
            params={},
        )
        with pytest.raises(ValueError, match="different targets"):
            store.create_run(
                scenario="s",
                request_id="req-ct",
                targets=["d2"],
                params={},
            )

    def test_different_parent_raises(self, store):
        store.create_run(
            scenario="s",
            request_id="req-par",
            targets=["d1"],
            params={},
            parent_run_id="p1",
        )
        with pytest.raises(ValueError, match="different snapshot"):
            store.create_run(
                scenario="s",
                request_id="req-par",
                targets=["d1"],
                params={},
                parent_run_id="p2",
            )


# ---------------------------------------------------------------------------
# record_target
# ---------------------------------------------------------------------------

class TestRecordTarget:
    def _make_result(self, status="succeeded", **extra):
        base = {
            "status": status,
            "step": "done",
            "code": 0,
            "last_ok_step": "verify",
            "safe_retry": False,
            "route": "default",
            "steps": 5,
            "battery": 80,
        }
        base.update(extra)
        return base

    def test_record_success(self, store):
        rid = store.create_run(scenario="s", request_id="r1", targets=["d1"], params={})
        store.record_target(rid, "d1", self._make_result("succeeded"))
        run = store.get_run(rid)
        assert run["status"] == "succeeded"
        t = run["targets"][0]
        assert t["status"] == "succeeded"
        assert t["result"]["status"] == "succeeded"

    def test_record_failed(self, store):
        rid = store.create_run(scenario="s", request_id="r2", targets=["d1"], params={})
        store.record_target(rid, "d1", self._make_result("failed"))
        assert store.get_run(rid)["status"] == "failed"

    def test_record_cancelled(self, store):
        rid = store.create_run(scenario="s", request_id="r3", targets=["d1"], params={})
        store.record_target(rid, "d1", self._make_result("cancelled"))
        assert store.get_run(rid)["status"] == "cancelled"

    def test_all_result_fields_stored(self, store):
        rid = store.create_run(scenario="s", request_id="r4", targets=["d1"], params={})
        result = {
            "status": "succeeded",
            "step": "run",
            "code": 0,
            "last_ok_step": "boot",
            "safe_retry": True,
            "route": "express",
            "steps": 3,
            "battery": 90,
            "device_id": "d1",
        }
        store.record_target(rid, "d1", result)
        t = store.get_run(rid)["targets"][0]
        assert t["result"] == result

    def test_unknown_result_key_raises(self, store):
        rid = store.create_run(scenario="s", request_id="r5", targets=["d1"], params={})
        with pytest.raises(ValueError, match="Unknown result keys"):
            store.record_target(rid, "d1", {"status": "succeeded", "hack": "x"})

    def test_invalid_status_raises(self, store):
        rid = store.create_run(scenario="s", request_id="r6", targets=["d1"], params={})
        with pytest.raises(ValueError, match="status must be one of"):
            store.record_target(rid, "d1", {"status": "running"})

    def test_non_target_device_raises(self, store):
        rid = store.create_run(scenario="s", request_id="r7", targets=["d1"], params={})
        with pytest.raises(ValueError, match="not a target"):
            store.record_target(rid, "d9", {"status": "succeeded"})

    def test_identical_replay_noop(self, store):
        rid = store.create_run(scenario="s", request_id="r8", targets=["d1"], params={})
        result = self._make_result("succeeded")
        store.record_target(rid, "d1", result)
        # Second identical call must not raise
        store.record_target(rid, "d1", result)
        assert store.get_run(rid)["status"] == "succeeded"

    def test_conflicting_replay_raises(self, store):
        rid = store.create_run(scenario="s", request_id="r9", targets=["d1"], params={})
        store.record_target(rid, "d1", self._make_result("succeeded"))
        with pytest.raises(ValueError, match="already has a terminal result"):
            store.record_target(rid, "d1", self._make_result("failed"))


# ---------------------------------------------------------------------------
# Mixed results → aggregate status
# ---------------------------------------------------------------------------

class TestMixedResults:
    def _result(self, status):
        return {"status": status, "step": "x", "code": 0,
                "last_ok_step": "x", "safe_retry": False,
                "route": "r", "steps": 1, "battery": 100}

    def test_partial_when_some_succeed_some_fail(self, store):
        rid = store.create_run(
            scenario="s", request_id="mix1",
            targets=["d1", "d2", "d3"], params={}
        )
        store.record_target(rid, "d1", self._result("succeeded"))
        store.record_target(rid, "d2", self._result("failed"))
        store.record_target(rid, "d3", self._result("cancelled"))
        assert store.get_run(rid)["status"] == "partial"

    def test_partial_when_some_succeed_some_queued(self, store):
        rid = store.create_run(
            scenario="s", request_id="mix2",
            targets=["d1", "d2"], params={}
        )
        store.record_target(rid, "d1", self._result("succeeded"))
        run = store.get_run(rid)
        # One done, one queued → partial
        assert run["status"] == "partial"

    def test_failed_when_all_fail(self, store):
        rid = store.create_run(
            scenario="s", request_id="mix3",
            targets=["d1", "d2"], params={}
        )
        store.record_target(rid, "d1", self._result("failed"))
        store.record_target(rid, "d2", self._result("failed"))
        assert store.get_run(rid)["status"] == "failed"

    def test_succeeded_when_all_succeed(self, store):
        rid = store.create_run(
            scenario="s", request_id="mix4",
            targets=["d1", "d2"], params={}
        )
        store.record_target(rid, "d1", self._result("succeeded"))
        store.record_target(rid, "d2", self._result("succeeded"))
        assert store.get_run(rid)["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Persistence across reopen
# ---------------------------------------------------------------------------

class TestPersistence:
    def _result(self, status="succeeded"):
        return {"status": status, "step": "done", "code": 0,
                "last_ok_step": "boot", "safe_retry": False,
                "route": "main", "steps": 2, "battery": 75}

    def test_run_survives_reopen(self, db_path):
        with RunStore(db_path) as s:
            rid = s.create_run(
                scenario="persist-test",
                request_id="p-req-1",
                targets=["alpha", "beta", "gamma"],
                params={"min_battery": 20},
            )
            s.record_target(rid, "alpha", self._result("succeeded"))

        # Reopen and verify
        with RunStore(db_path) as s2:
            run = s2.get_run(rid)
            assert run is not None
            assert run["scenario"] == "persist-test"
            assert run["params"] == {"min_battery": 20}
            assert run["status"] == "partial"
            order = [t["device_id"] for t in run["targets"]]
            assert order == ["alpha", "beta", "gamma"]
            assert run["targets"][0]["status"] == "succeeded"
            assert run["targets"][1]["status"] == "queued"

    def test_target_order_preserved_after_reopen(self, db_path):
        devices = [f"dev-{i}" for i in range(10)]
        with RunStore(db_path) as s:
            rid = s.create_run(
                scenario="order-test",
                request_id="ord-req",
                targets=devices,
                params={},
            )
            for d in devices[5:]:
                s.record_target(rid, d, self._result("succeeded"))

        with RunStore(db_path) as s2:
            run = s2.get_run(rid)
            assert [t["device_id"] for t in run["targets"]] == devices

    def test_idempotent_create_after_reopen(self, db_path):
        with RunStore(db_path) as s:
            rid1 = s.create_run(
                scenario="idem",
                request_id="idem-req",
                targets=["x", "y"],
                params={"parallel": 3},
            )

        with RunStore(db_path) as s2:
            rid2 = s2.create_run(
                scenario="idem",
                request_id="idem-req",
                targets=["x", "y"],
                params={"parallel": 3},
            )
            assert rid1 == rid2

    def test_conflict_detected_after_reopen(self, db_path):
        with RunStore(db_path) as s:
            s.create_run(
                scenario="orig",
                request_id="conf-req",
                targets=["d1"],
                params={},
            )

        with RunStore(db_path) as s2:
            with pytest.raises(ValueError, match="different snapshot"):
                s2.create_run(
                    scenario="changed",
                    request_id="conf-req",
                    targets=["d1"],
                    params={},
                )
