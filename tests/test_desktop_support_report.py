"""
Tests for desktop/support_report.py

Coverage:
- Valid round-trip (build then write then read-back)
- Synthetic secrets in extra/nested fields are excluded
- Weird/crafted version strings are rejected
- Direct unsafe writer input is rejected (extra keys, secrets)
- Failure leaves no temp file behind
- All major allowlist/boundary paths
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from desktop.support_report import build_support_report, write_support_archive


# ---------------------------------------------------------------------------
# Minimal valid fixtures
# ---------------------------------------------------------------------------

VALID_SETUP = {
    "status": "ok",
    "ready_count": 2,
    "unauthorized_count": 0,
    "offline_count": 0,
    "ambiguous_count": 0,
    "unverified_count": 0,
    "other_count": 0,
}

VALID_VERSIONS = {"adb": "1.0.41", "scrcpy": "2.3"}

VALID_RUN = {
    "status": "done",
    "targets": [
        {
            "device_id": "REALME123ABC",
            "status": "done",
            "result": {
                "battery": 85,
                "code": None,
                "step": "done",
                "last_ok_step": "done",
                "safe_retry": False,
            },
        },
        {
            "device_id": "REDMI_SECRET_SN",
            "status": "failed",
            "result": {
                "battery": 10,
                "code": "BATTERY_LOW",
                "step": "battery_check",
                "last_ok_step": "probe",
                "safe_retry": True,
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_zip_json(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("diagnostics.json"))


# ---------------------------------------------------------------------------
# Valid round-trip
# ---------------------------------------------------------------------------


class TestValidRoundTrip:
    def test_build_returns_schema_version(self):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        assert report["schema_version"] == 1

    def test_build_includes_setup_fields(self):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        s = report["setup"]
        assert s["status"] == "ok"
        assert s["ready_count"] == 2
        assert s["unauthorized_count"] == 0

    def test_build_includes_versions(self):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        assert report["versions"]["adb"] == "1.0.41"
        assert report["versions"]["scrcpy"] == "2.3"

    def test_build_run_none(self):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        assert report["run"] is None

    def test_build_with_run(self):
        report = build_support_report(
            setup=VALID_SETUP, versions=VALID_VERSIONS, run=VALID_RUN
        )
        r = report["run"]
        assert r["status"] == "done"
        assert len(r["targets"]) == 2

    def test_device_ids_replaced_with_labels(self):
        report = build_support_report(
            setup=VALID_SETUP, versions=VALID_VERSIONS, run=VALID_RUN
        )
        targets = report["run"]["targets"]
        labels = [t["device_label"] for t in targets]
        assert labels == ["Device 1", "Device 2"]
        raw_text = json.dumps(report)
        assert "REALME123ABC" not in raw_text
        assert "REDMI_SECRET_SN" not in raw_text

    def test_result_fields_preserved(self):
        report = build_support_report(
            setup=VALID_SETUP, versions=VALID_VERSIONS, run=VALID_RUN
        )
        t0 = report["run"]["targets"][0]
        assert t0["result"]["battery"] == 85
        assert t0["result"]["code"] is None
        assert t0["result"]["step"] == "done"
        assert t0["result"]["safe_retry"] is False

        t1 = report["run"]["targets"][1]
        assert t1["result"]["battery"] == 10
        assert t1["result"]["code"] == "BATTERY_LOW"
        assert t1["result"]["safe_retry"] is True

    def test_write_produces_zip(self, tmp_path):
        report = build_support_report(
            setup=VALID_SETUP, versions=VALID_VERSIONS, run=VALID_RUN
        )
        dest = tmp_path / "support.zip"
        result = write_support_archive(dest, report)
        assert result == dest
        assert dest.exists()
        assert zipfile.is_zipfile(dest)

    def test_write_zip_contains_diagnostics_json(self, tmp_path):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        dest = tmp_path / "support.zip"
        write_support_archive(dest, report)
        with zipfile.ZipFile(dest) as zf:
            assert "diagnostics.json" in zf.namelist()

    def test_write_json_valid_utf8(self, tmp_path):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        dest = tmp_path / "support.zip"
        write_support_archive(dest, report)
        data = _read_zip_json(dest)
        assert data["schema_version"] == 1

    def test_write_round_trip_all_fields(self, tmp_path):
        report = build_support_report(
            setup=VALID_SETUP, versions=VALID_VERSIONS, run=VALID_RUN
        )
        dest = tmp_path / "support.zip"
        write_support_archive(dest, report)
        data = _read_zip_json(dest)
        assert data["schema_version"] == 1
        assert data["setup"]["status"] == "ok"
        assert data["versions"]["adb"] == "1.0.41"
        assert data["run"]["status"] == "done"
        assert data["run"]["targets"][0]["device_label"] == "Device 1"

    def test_write_creates_parent_dirs(self, tmp_path):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        dest = tmp_path / "a" / "b" / "support.zip"
        write_support_archive(dest, report)
        assert dest.exists()

    def test_write_atomic_replace(self, tmp_path):
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS)
        dest = tmp_path / "support.zip"
        # Write twice — second should replace atomically.
        write_support_archive(dest, report)
        write_support_archive(dest, report)
        assert dest.exists()


# ---------------------------------------------------------------------------
# Secret / extra-key exclusion
# ---------------------------------------------------------------------------


class TestSecretExclusion:
    def test_extra_top_level_setup_keys_excluded(self):
        setup = {**VALID_SETUP, "token": "SECRET_TOKEN_123", "serial": "DEADBEEF"}
        report = build_support_report(setup=setup, versions=VALID_VERSIONS)
        raw = json.dumps(report)
        assert "SECRET_TOKEN_123" not in raw
        assert "token" not in raw
        assert "DEADBEEF" not in raw
        assert "serial" not in raw

    def test_extra_nested_result_keys_excluded(self):
        run = {
            "status": "done",
            "targets": [
                {
                    "device_id": "ABC",
                    "status": "done",
                    "result": {
                        "battery": 50,
                        "code": None,
                        "step": "done",
                        "last_ok_step": "done",
                        "safe_retry": False,
                        "log": "sensitive raw log data",
                        "screenshot_path": "/home/user/screen.png",
                        "input_text": "my password",
                    },
                }
            ],
        }
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS, run=run)
        raw = json.dumps(report)
        assert "sensitive raw log data" not in raw
        assert "log" not in raw
        assert "/home/user/screen.png" not in raw
        assert "screenshot_path" not in raw
        assert "my password" not in raw
        assert "input_text" not in raw

    def test_device_id_serial_not_in_output(self):
        run = {
            "status": "done",
            "targets": [
                {"device_id": "MY_SERIAL_S123", "status": "done", "result": None}
            ],
        }
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS, run=run)
        raw = json.dumps(report)
        assert "MY_SERIAL_S123" not in raw

    def test_extra_run_level_keys_excluded(self):
        run = {
            "status": "done",
            "targets": [],
            "run_id": "RUN-XYZ-SECRET",
            "environment": {"HOME": "/root", "PATH": "/usr/bin"},
            "metadata": {"user": "alice"},
        }
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS, run=run)
        raw = json.dumps(report)
        assert "RUN-XYZ-SECRET" not in raw
        assert "run_id" not in raw
        assert "environment" not in raw
        assert "alice" not in raw

    def test_target_extra_keys_excluded(self):
        run = {
            "status": "done",
            "targets": [
                {
                    "device_id": "DEV1",
                    "status": "done",
                    "result": None,
                    "url": "https://example.com/secret",
                    "request_id": "req-999",
                }
            ],
        }
        report = build_support_report(setup=VALID_SETUP, versions=VALID_VERSIONS, run=run)
        raw = json.dumps(report)
        assert "https://example.com/secret" not in raw
        assert "request_id" not in raw


# ---------------------------------------------------------------------------
# Version validation
# ---------------------------------------------------------------------------


class TestVersionValidation:
    @pytest.mark.parametrize(
        "good",
        ["1.0.41", "2.3", "10.0.0.1", "0", "31.1.3", "1.0"],
    )
    def test_valid_versions_accepted(self, good):
        report = build_support_report(
            setup=VALID_SETUP, versions={"adb": good}
        )
        assert report["versions"]["adb"] == good

    @pytest.mark.parametrize(
        "bad",
        [
            "1.0.41-dev",          # has suffix
            "v1.0.41",             # has prefix letter
            "1.0.41 (stable)",     # has description
            "latest",              # arbitrary string
            "",                    # empty
            "1.2.3.4.5",           # too many components
            "1." * 11,             # way too long
            "adb version 1.0.41",  # full sentence
            "1000.0",              # component out of range
        ],
    )
    def test_invalid_versions_rejected(self, bad):
        with pytest.raises(ValueError):
            build_support_report(setup=VALID_SETUP, versions={"adb": bad})

    def test_non_string_version_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(setup=VALID_SETUP, versions={"adb": 141})

    def test_unknown_tool_name_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions={"adb": "1.0.41", "nmap": "7.94"},
            )

    def test_empty_versions_accepted(self):
        report = build_support_report(setup=VALID_SETUP, versions={})
        assert report["versions"] == {}


# ---------------------------------------------------------------------------
# Setup validation
# ---------------------------------------------------------------------------


class TestSetupValidation:
    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup={**VALID_SETUP, "status": "partially_ok"},
                versions=VALID_VERSIONS,
            )

    def test_negative_count_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup={**VALID_SETUP, "ready_count": -1},
                versions=VALID_VERSIONS,
            )

    def test_float_count_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup={**VALID_SETUP, "ready_count": 1.5},
                versions=VALID_VERSIONS,
            )

    def test_missing_counts_default_to_zero(self):
        report = build_support_report(
            setup={"status": "ok"}, versions=VALID_VERSIONS
        )
        assert report["setup"]["ready_count"] == 0
        assert report["setup"]["other_count"] == 0

    def test_all_valid_statuses_accepted(self):
        for status in ("ok", "degraded", "error", "unknown"):
            report = build_support_report(
                setup={**VALID_SETUP, "status": status},
                versions=VALID_VERSIONS,
            )
            assert report["setup"]["status"] == status

    def test_bool_as_int_count_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup={**VALID_SETUP, "ready_count": True},
                versions=VALID_VERSIONS,
            )


# ---------------------------------------------------------------------------
# Run / result validation
# ---------------------------------------------------------------------------


class TestRunValidation:
    def test_invalid_run_status_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={"status": "in_progress", "targets": []},
            )

    def test_invalid_device_status_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={
                    "status": "done",
                    "targets": [
                        {"device_id": "D1", "status": "FINISHED", "result": None}
                    ],
                },
            )

    def test_invalid_error_code_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={
                    "status": "done",
                    "targets": [
                        {
                            "device_id": "D1",
                            "status": "failed",
                            "result": {
                                "battery": 50,
                                "code": "CUSTOM_ERROR",
                                "step": "probe",
                                "last_ok_step": "init",
                                "safe_retry": False,
                            },
                        }
                    ],
                },
            )

    def test_allowed_error_codes_pass(self):
        for code in (
            "USB_DISCONNECTED",
            "ADB_UNAUTHORIZED",
            "TIMEOUT",
            "PROBE_FAILED",
            "BATTERY_LOW",
            "RUN_INTERRUPTED",
        ):
            report = build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={
                    "status": "failed",
                    "targets": [
                        {
                            "device_id": "D1",
                            "status": "failed",
                            "result": {
                                "battery": 50,
                                "code": code,
                                "step": "probe",
                                "last_ok_step": "init",
                                "safe_retry": False,
                            },
                        }
                    ],
                },
            )
            assert report["run"]["targets"][0]["result"]["code"] == code

    def test_invalid_step_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={
                    "status": "done",
                    "targets": [
                        {
                            "device_id": "D1",
                            "status": "done",
                            "result": {
                                "battery": 50,
                                "code": None,
                                "step": "HACKING",
                                "last_ok_step": "init",
                                "safe_retry": False,
                            },
                        }
                    ],
                },
            )

    def test_battery_above_100_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={
                    "status": "done",
                    "targets": [
                        {
                            "device_id": "D1",
                            "status": "done",
                            "result": {
                                "battery": 101,
                                "code": None,
                                "step": "done",
                                "last_ok_step": "done",
                                "safe_retry": False,
                            },
                        }
                    ],
                },
            )

    def test_safe_retry_non_bool_rejected(self):
        with pytest.raises(ValueError):
            build_support_report(
                setup=VALID_SETUP,
                versions=VALID_VERSIONS,
                run={
                    "status": "done",
                    "targets": [
                        {
                            "device_id": "D1",
                            "status": "done",
                            "result": {
                                "battery": 50,
                                "code": None,
                                "step": "done",
                                "last_ok_step": "done",
                                "safe_retry": "yes",
                            },
                        }
                    ],
                },
            )

    def test_null_result_ok(self):
        report = build_support_report(
            setup=VALID_SETUP,
            versions=VALID_VERSIONS,
            run={
                "status": "pending",
                "targets": [{"device_id": "D1", "status": "pending", "result": None}],
            },
        )
        assert report["run"]["targets"][0]["result"] is None


# ---------------------------------------------------------------------------
# Writer safety — direct unsafe input
# ---------------------------------------------------------------------------


class TestWriterSafety:
    def test_writer_rejects_extra_keys_in_setup(self, tmp_path):
        """Caller cannot smuggle extra keys by passing a crafted report dict."""
        malicious_report = {
            "schema_version": 1,
            "setup": {
                **VALID_SETUP,
                "secret_token": "SUPER_SECRET",
            },
            "versions": VALID_VERSIONS,
            "run": None,
        }
        dest = tmp_path / "out.zip"
        # Writer silently excludes extra setup keys (they are ignored, not in output).
        write_support_archive(dest, malicious_report)
        data = _read_zip_json(dest)
        assert "secret_token" not in data["setup"]
        assert "SUPER_SECRET" not in json.dumps(data)

    def test_writer_rejects_wrong_schema_version(self, tmp_path):
        malicious_report = {
            "schema_version": 99,
            "setup": VALID_SETUP,
            "versions": VALID_VERSIONS,
            "run": None,
        }
        with pytest.raises(ValueError, match="schema_version"):
            write_support_archive(tmp_path / "out.zip", malicious_report)

    def test_writer_rejects_bad_version_string(self, tmp_path):
        crafted_report = {
            "schema_version": 1,
            "setup": VALID_SETUP,
            "versions": {"adb": "1.0.41; rm -rf /"},
            "run": None,
        }
        with pytest.raises(ValueError):
            write_support_archive(tmp_path / "out.zip", crafted_report)

    def test_writer_rejects_unknown_tool(self, tmp_path):
        crafted_report = {
            "schema_version": 1,
            "setup": VALID_SETUP,
            "versions": {"adb": "1.0.41", "evil_tool": "0.1"},
            "run": None,
        }
        with pytest.raises(ValueError):
            write_support_archive(tmp_path / "out.zip", crafted_report)

    def test_writer_rejects_bad_device_label(self, tmp_path):
        crafted_report = {
            "schema_version": 1,
            "setup": VALID_SETUP,
            "versions": VALID_VERSIONS,
            "run": {
                "status": "done",
                "targets": [
                    {
                        "device_label": "serial-DEADBEEF",  # not "Device N"
                        "status": "done",
                        "result": None,
                    }
                ],
            },
        }
        with pytest.raises(ValueError):
            write_support_archive(tmp_path / "out.zip", crafted_report)

    def test_writer_rejects_non_mapping(self, tmp_path):
        with pytest.raises((ValueError, AttributeError, TypeError)):
            write_support_archive(tmp_path / "out.zip", "not a mapping")  # type: ignore[arg-type]

    def test_writer_excludes_nested_secrets_in_result(self, tmp_path):
        """Extra result keys from a crafted mapping are excluded."""
        report_dict = build_support_report(
            setup=VALID_SETUP, versions=VALID_VERSIONS, run=VALID_RUN
        )
        # Inject a secret into an already-labelled result dict before writing.
        import copy

        crafted = copy.deepcopy(report_dict)
        crafted["run"]["targets"][0]["result"]["secret_key"] = "HIDDEN_VALUE"
        dest = tmp_path / "out.zip"
        write_support_archive(dest, crafted)
        data = _read_zip_json(dest)
        raw = json.dumps(data)
        assert "HIDDEN_VALUE" not in raw
        assert "secret_key" not in raw


# ---------------------------------------------------------------------------
# Failure cleanup
# ---------------------------------------------------------------------------


class TestFailureCleanup:
    def test_no_temp_file_left_after_bad_schema_version(self, tmp_path):
        bad_report = {
            "schema_version": 999,
            "setup": VALID_SETUP,
            "versions": VALID_VERSIONS,
            "run": None,
        }
        dest = tmp_path / "fail.zip"
        with pytest.raises(ValueError):
            write_support_archive(dest, bad_report)
        # Destination must not be created.
        assert not dest.exists()
        # No leftover temp files.
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_no_temp_file_left_after_bad_version_string(self, tmp_path):
        bad_report = {
            "schema_version": 1,
            "setup": VALID_SETUP,
            "versions": {"adb": "NOT_A_VERSION"},
            "run": None,
        }
        dest = tmp_path / "fail.zip"
        with pytest.raises(ValueError):
            write_support_archive(dest, bad_report)
        assert not dest.exists()
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []
