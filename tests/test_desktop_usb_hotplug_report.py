#!/usr/bin/env python3
"""Comprehensive test suite for desktop_usb_hotplug_report.py."""

from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

# Ensure repository root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.desktop_usb_hotplug_report import (
    CycleEdgeDetail,
    EdgeSummary,
    HotplugError,
    HotplugRecord,
    HotplugSchemaError,
    HotplugSyntaxError,
    ReportData,
    compute_metrics,
    generate_report,
    main,
    nearest_rank_percentile,
    parse_jsonl_file,
    parse_line,
    validate_record_dict,
)


class TestNearestRankPercentile(unittest.TestCase):
    """Tests for nearest rank percentile calculation."""

    def test_single_value(self) -> None:
        vals = [4.2]
        self.assertEqual(nearest_rank_percentile(vals, 50.0), 4.2)
        self.assertEqual(nearest_rank_percentile(vals, 95.0), 4.2)
        self.assertEqual(nearest_rank_percentile(vals, 100.0), 4.2)

    def test_known_dataset_ten_values(self) -> None:
        # Values 1 to 10
        vals = [float(i) for i in range(1, 11)]
        # p50: ceil(0.50 * 10) = 5 -> index 4 -> 5.0
        self.assertEqual(nearest_rank_percentile(vals, 50.0), 5.0)
        # p95: ceil(0.95 * 10) = ceil(9.5) = 10 -> index 9 -> 10.0
        self.assertEqual(nearest_rank_percentile(vals, 95.0), 10.0)
        # p100: ceil(1.00 * 10) = 10 -> 10.0
        self.assertEqual(nearest_rank_percentile(vals, 100.0), 10.0)

    def test_thirty_values_rank(self) -> None:
        vals = [float(i) for i in range(1, 31)]
        # p50: ceil(0.50 * 30) = 15 -> index 14 -> 15.0
        self.assertEqual(nearest_rank_percentile(vals, 50.0), 15.0)
        # p95: ceil(0.95 * 30) = ceil(28.5) = 29 -> index 28 -> 29.0
        self.assertEqual(nearest_rank_percentile(vals, 95.0), 29.0)

    def test_negative_values(self) -> None:
        # Differences like adb->park can be negative
        vals = [-3.0, -1.5, -0.5, 0.0, 1.0, 2.0]
        # p50: ceil(0.50 * 6) = 3 -> index 2 -> -0.5
        self.assertEqual(nearest_rank_percentile(vals, 50.0), -0.5)
        # p95: ceil(0.95 * 6) = ceil(5.7) = 6 -> index 5 -> 2.0
        self.assertEqual(nearest_rank_percentile(vals, 95.0), 2.0)

    def test_empty_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            nearest_rank_percentile([], 50.0)

    def test_compute_metrics_empty(self) -> None:
        metrics = compute_metrics([])
        self.assertIsNone(metrics["p50"])
        self.assertIsNone(metrics["p95"])
        self.assertIsNone(metrics["max"])


class TestLineParsingAndValidation(unittest.TestCase):
    """Tests for JSONL syntax and schema validation."""

    def test_valid_record(self) -> None:
        line = '{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 10.5}'
        rec = parse_line(line, 1)
        self.assertEqual(rec.line_num, 1)
        self.assertEqual(rec.kind, "marker")
        self.assertEqual(rec.cycle, 1)
        self.assertEqual(rec.edge, "disconnect")
        self.assertEqual(rec.elapsed_sec, 10.5)

    def test_valid_zero_elapsed(self) -> None:
        line = '{"kind": "adb", "cycle": 2, "edge": "reconnect", "elapsed_sec": 0}'
        rec = parse_line(line, 5)
        self.assertEqual(rec.elapsed_sec, 0.0)

    def test_empty_line_syntax_error(self) -> None:
        with self.assertRaises(HotplugSyntaxError) as ctx:
            parse_line("   \n", 42)
        self.assertEqual(ctx.exception.line_num, 42)
        self.assertIn("empty line", ctx.exception.message)

    def test_malformed_json_syntax_error(self) -> None:
        with self.assertRaises(HotplugSyntaxError) as ctx:
            parse_line('{kind: "marker", cycle: 1}', 10)
        self.assertEqual(ctx.exception.line_num, 10)

    def test_not_a_json_object(self) -> None:
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line('["marker", 1, "disconnect", 0.0]', 3)
        self.assertEqual(ctx.exception.line_num, 3)
        self.assertIn("expected JSON object", ctx.exception.message)

    def test_missing_required_field(self) -> None:
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line('{"kind": "marker", "cycle": 1, "edge": "disconnect"}', 7)
        self.assertEqual(ctx.exception.line_num, 7)
        self.assertIn("missing required fields", ctx.exception.message)
        self.assertIn("elapsed_sec", ctx.exception.message)

    def test_unexpected_field(self) -> None:
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0, "user": "alice"}',
                8,
            )
        self.assertEqual(ctx.exception.line_num, 8)
        self.assertIn("unexpected fields", ctx.exception.message)

    def test_invalid_kind(self) -> None:
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "sensor", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0}',
                9,
            )
        self.assertEqual(ctx.exception.line_num, 9)
        self.assertIn("invalid kind", ctx.exception.message)

    def test_invalid_edge(self) -> None:
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": 1, "edge": "plug", "elapsed_sec": 1.0}',
                12,
            )
        self.assertEqual(ctx.exception.line_num, 12)
        self.assertIn("invalid edge", ctx.exception.message)

    def test_invalid_cycle_zero_or_negative(self) -> None:
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": 0, "edge": "disconnect", "elapsed_sec": 1.0}',
                15,
            )
        self.assertEqual(ctx.exception.line_num, 15)
        self.assertIn("cycle must be a positive integer", ctx.exception.message)

        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": -3, "edge": "disconnect", "elapsed_sec": 1.0}',
                16,
            )
        self.assertEqual(ctx.exception.line_num, 16)
        self.assertIn("cycle must be a positive integer", ctx.exception.message)

    def test_invalid_cycle_type(self) -> None:
        # Boolean is subclass of int in python, must be rejected
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": true, "edge": "disconnect", "elapsed_sec": 1.0}',
                17,
            )
        self.assertEqual(ctx.exception.line_num, 17)
        self.assertIn("cycle must be a positive integer", ctx.exception.message)

        # Float cycle
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": 1.5, "edge": "disconnect", "elapsed_sec": 1.0}',
                18,
            )
        self.assertEqual(ctx.exception.line_num, 18)
        self.assertIn("cycle must be a positive integer", ctx.exception.message)

    def test_invalid_elapsed_sec_negative_or_nan(self) -> None:
        # Negative
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": -0.5}',
                20,
            )
        self.assertEqual(ctx.exception.line_num, 20)
        self.assertIn("elapsed_sec must be a finite non-negative number", ctx.exception.message)

        # Boolean elapsed_sec
        with self.assertRaises(HotplugSchemaError) as ctx:
            parse_line(
                '{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": false}',
                21,
            )
        self.assertEqual(ctx.exception.line_num, 21)

        # Non-finite elapsed_sec via dict
        with self.assertRaises(HotplugSchemaError):
            validate_record_dict(
                {"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": float("nan")},
                22,
            )
        with self.assertRaises(HotplugSchemaError):
            validate_record_dict(
                {"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": float("inf")},
                23,
            )


class TestOrderAndNegativeDifference(unittest.TestCase):
    """Tests for observer ordering, negative difference, and observation before marker."""

    def test_standard_order_adb_before_park(self) -> None:
        # adb observed before park: t_adb < t_park -> adb->park diff > 0
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 10.0),
            HotplugRecord(2, "adb", 1, "disconnect", 10.4),
            HotplugRecord(3, "park", 1, "disconnect", 10.9),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertTrue(detail.is_valid)
        self.assertAlmostEqual(detail.marker_to_adb_sec, 0.4)
        self.assertAlmostEqual(detail.marker_to_park_sec, 0.9)
        self.assertAlmostEqual(detail.adb_to_park_sec, 0.5)
        self.assertEqual(detail.observation_order, "adb < park")
        self.assertEqual(rep.edges["disconnect"].observation_order_counts["adb_first"], 1)

    def test_inverted_order_park_before_adb(self) -> None:
        # park observed before adb: t_park < t_adb -> adb->park diff < 0
        # Requirement: "Если park раньше adb, обе физические задержки всё равно валидны
        # при timestamps >= marker, для отрицательной разницы отдельно отметь порядок наблюдений."
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 10.0),
            HotplugRecord(2, "adb", 1, "disconnect", 11.2),
            HotplugRecord(3, "park", 1, "disconnect", 10.5),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertTrue(detail.is_valid)
        self.assertAlmostEqual(detail.marker_to_adb_sec, 1.2)
        self.assertAlmostEqual(detail.marker_to_park_sec, 0.5)
        self.assertAlmostEqual(detail.adb_to_park_sec, -0.7)
        self.assertEqual(detail.observation_order, "park < adb")
        self.assertIn("negative adb->park diff", detail.order_note)
        self.assertEqual(rep.edges["disconnect"].observation_order_counts["park_first"], 1)

    def test_simultaneous_observations(self) -> None:
        records = [
            HotplugRecord(1, "marker", 1, "reconnect", 20.0),
            HotplugRecord(2, "adb", 1, "reconnect", 20.5),
            HotplugRecord(3, "park", 1, "reconnect", 20.5),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = [c for c in rep.cycles if c.edge == "reconnect"][0]
        self.assertTrue(detail.is_valid)
        self.assertAlmostEqual(detail.adb_to_park_sec, 0.0)
        self.assertEqual(detail.observation_order, "adb == park")
        self.assertEqual(rep.edges["reconnect"].observation_order_counts["simultaneous"], 1)

    def test_observation_earlier_than_marker_adb(self) -> None:
        # ADB observed before marker: t_adb < t_marker
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 10.0),
            HotplugRecord(2, "adb", 1, "disconnect", 9.8),
            HotplugRecord(3, "park", 1, "disconnect", 10.5),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertFalse(detail.is_valid)
        self.assertIn("observation before marker", detail.anomalies[0])
        self.assertEqual(rep.edges["disconnect"].valid_count, 0)
        self.assertEqual(rep.anomalies_summary["observations_before_marker"], 1)

    def test_observation_earlier_than_marker_park(self) -> None:
        # Park observed before marker: t_park < t_marker
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 10.0),
            HotplugRecord(2, "adb", 1, "disconnect", 10.5),
            HotplugRecord(3, "park", 1, "disconnect", 9.9),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertFalse(detail.is_valid)
        self.assertIn("observation before marker", detail.anomalies[0])
        self.assertEqual(rep.edges["disconnect"].valid_count, 0)
        self.assertEqual(rep.anomalies_summary["observations_before_marker"], 1)


class TestAnomaliesAndExclusions(unittest.TestCase):
    """Tests for incomplete sets, duplicates, and missing cycle handling."""

    def test_incomplete_set_missing_park(self) -> None:
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 1.0),
            HotplugRecord(2, "adb", 1, "disconnect", 1.5),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertFalse(detail.is_valid)
        self.assertIn("incomplete set: missing park", detail.anomalies[0])
        self.assertEqual(rep.edges["disconnect"].valid_count, 0)
        self.assertEqual(rep.anomalies_summary["incomplete_sets"], 1)

    def test_incomplete_set_missing_marker(self) -> None:
        records = [
            HotplugRecord(1, "adb", 1, "disconnect", 1.5),
            HotplugRecord(2, "park", 1, "disconnect", 1.8),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertFalse(detail.is_valid)
        self.assertIn("incomplete set: missing marker", detail.anomalies[0])
        self.assertEqual(rep.edges["disconnect"].valid_count, 0)

    def test_duplicate_adb(self) -> None:
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 1.0),
            HotplugRecord(2, "adb", 1, "disconnect", 1.4),
            HotplugRecord(3, "adb", 1, "disconnect", 1.6),
            HotplugRecord(4, "park", 1, "disconnect", 1.8),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertFalse(detail.is_valid)
        self.assertTrue(any("duplicate adb" in a for a in detail.anomalies))
        self.assertEqual(rep.edges["disconnect"].valid_count, 0)
        self.assertEqual(rep.anomalies_summary["duplicates"], 1)

    def test_duplicate_marker(self) -> None:
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 1.0),
            HotplugRecord(2, "marker", 1, "disconnect", 1.1),
            HotplugRecord(3, "adb", 1, "disconnect", 1.4),
            HotplugRecord(4, "park", 1, "disconnect", 1.8),
        ]
        rep = generate_report(records, criterion_min_samples=1)
        detail = rep.cycles[0]
        self.assertFalse(detail.is_valid)
        self.assertTrue(any("duplicate marker" in a for a in detail.anomalies))
        self.assertEqual(rep.edges["disconnect"].valid_count, 0)
        self.assertEqual(rep.anomalies_summary["duplicates"], 1)

    def test_skipped_cycle_gap_not_masked(self) -> None:
        # File has cycle 1 and cycle 3, cycle 2 is completely skipped
        records = [
            # Cycle 1 disconnect & reconnect
            HotplugRecord(1, "marker", 1, "disconnect", 1.0),
            HotplugRecord(2, "adb", 1, "disconnect", 1.2),
            HotplugRecord(3, "park", 1, "disconnect", 1.4),
            HotplugRecord(4, "marker", 1, "reconnect", 2.0),
            HotplugRecord(5, "adb", 1, "reconnect", 2.2),
            HotplugRecord(6, "park", 1, "reconnect", 2.4),
            # Cycle 3 disconnect & reconnect
            HotplugRecord(7, "marker", 3, "disconnect", 5.0),
            HotplugRecord(8, "adb", 3, "disconnect", 5.2),
            HotplugRecord(9, "park", 3, "disconnect", 5.4),
            HotplugRecord(10, "marker", 3, "reconnect", 6.0),
            HotplugRecord(11, "adb", 3, "reconnect", 6.2),
            HotplugRecord(12, "park", 3, "reconnect", 6.4),
        ]
        rep = generate_report(records, criterion_min_samples=2)
        # Cycle 2 should be in rep.cycles as invalid (omitted)
        c2_disc = [c for c in rep.cycles if c.cycle == 2 and c.edge == "disconnect"][0]
        self.assertFalse(c2_disc.is_valid)
        self.assertIn("cycle transition missing entirely", c2_disc.anomalies[0])
        # Only 2 valid observations per edge
        self.assertEqual(rep.edges["disconnect"].valid_count, 2)
        self.assertEqual(rep.edges["reconnect"].valid_count, 2)


class TestAcceptanceCriterionThresholds(unittest.TestCase):
    """Tests for threshold boundaries: N=29 vs N=30, p95 <= 4.0s vs p95 > 4.0s."""

    def _generate_valid_dataset(
        self,
        num_cycles: int,
        disconnect_park_delay: float = 2.0,
        reconnect_park_delay: float = 2.0,
    ) -> List[HotplugRecord]:
        records: List[HotplugRecord] = []
        line_num = 1
        for c in range(1, num_cycles + 1):
            t_base = float(c * 20)
            # disconnect
            records.append(HotplugRecord(line_num, "marker", c, "disconnect", t_base))
            line_num += 1
            records.append(HotplugRecord(line_num, "adb", c, "disconnect", t_base + 1.0))
            line_num += 1
            records.append(HotplugRecord(line_num, "park", c, "disconnect", t_base + disconnect_park_delay))
            line_num += 1

            # reconnect
            t_recon = t_base + 10.0
            records.append(HotplugRecord(line_num, "marker", c, "reconnect", t_recon))
            line_num += 1
            records.append(HotplugRecord(line_num, "adb", c, "reconnect", t_recon + 1.0))
            line_num += 1
            records.append(HotplugRecord(line_num, "park", c, "reconnect", t_recon + reconnect_park_delay))
            line_num += 1
        return records

    def test_sample_threshold_insufficient_29(self) -> None:
        # Exactly 29 observations: insufficient data
        records = self._generate_valid_dataset(29, disconnect_park_delay=2.0, reconnect_park_delay=2.0)
        rep = generate_report(records, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.overall_status, "INSUFFICIENT_DATA")
        self.assertEqual(rep.edges["disconnect"].status, "INSUFFICIENT_DATA")
        self.assertEqual(rep.edges["reconnect"].status, "INSUFFICIENT_DATA")

    def test_sample_threshold_one_edge_29_other_30(self) -> None:
        # Disconnect has 30, reconnect has 29
        records = self._generate_valid_dataset(30, disconnect_park_delay=2.0, reconnect_park_delay=2.0)
        # Remove cycle 30 reconnect park record (making reconnect cycle 30 incomplete)
        filtered = [
            r for r in records if not (r.cycle == 30 and r.edge == "reconnect" and r.kind == "park")
        ]
        rep = generate_report(filtered, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.edges["disconnect"].status, "PASSED")
        self.assertEqual(rep.edges["reconnect"].status, "INSUFFICIENT_DATA")
        self.assertEqual(rep.overall_status, "INSUFFICIENT_DATA")

    def test_metric_threshold_exact_boundary_4_0s_passes(self) -> None:
        # Exactly 30 observations, p95 marker->park == 4.0s -> PASSED
        records = self._generate_valid_dataset(30, disconnect_park_delay=4.0, reconnect_park_delay=4.0)
        rep = generate_report(records, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.edges["disconnect"].status, "PASSED")
        self.assertEqual(rep.edges["reconnect"].status, "PASSED")
        self.assertEqual(rep.overall_status, "PASSED")

    def test_metric_threshold_slightly_below_boundary_passes(self) -> None:
        # 30 observations, p95 marker->park == 3.999s -> PASSED
        records = self._generate_valid_dataset(30, disconnect_park_delay=3.999, reconnect_park_delay=3.999)
        rep = generate_report(records, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.overall_status, "PASSED")

    def test_metric_threshold_slightly_above_boundary_fails(self) -> None:
        # 30 observations, p95 marker->park == 4.001s -> FAILED
        records = self._generate_valid_dataset(30, disconnect_park_delay=4.001, reconnect_park_delay=2.0)
        rep = generate_report(records, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.edges["disconnect"].status, "FAILED")
        self.assertEqual(rep.edges["reconnect"].status, "PASSED")
        self.assertEqual(rep.overall_status, "FAILED")

    def test_both_edges_fail(self) -> None:
        records = self._generate_valid_dataset(30, disconnect_park_delay=4.5, reconnect_park_delay=5.0)
        rep = generate_report(records, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.edges["disconnect"].status, "FAILED")
        self.assertEqual(rep.edges["reconnect"].status, "FAILED")
        self.assertEqual(rep.overall_status, "FAILED")

    def test_anomalies_reduce_sample_count_causing_insufficient_data(self) -> None:
        # 35 cycles attempted, but 6 have anomalies -> 29 valid -> INSUFFICIENT_DATA
        records = self._generate_valid_dataset(35, disconnect_park_delay=2.0, reconnect_park_delay=2.0)
        # Invalidate 6 cycles of disconnect by making adb timestamp < marker timestamp
        modified_records: List[HotplugRecord] = []
        for r in records:
            if r.edge == "disconnect" and r.cycle in (1, 2, 3, 4, 5, 6) and r.kind == "adb":
                modified_records.append(HotplugRecord(r.line_num, r.kind, r.cycle, r.edge, 0.0))  # before marker
            else:
                modified_records.append(r)

        rep = generate_report(modified_records, criterion_threshold_sec=4.0, criterion_min_samples=30)
        self.assertEqual(rep.edges["disconnect"].valid_count, 29)
        self.assertEqual(rep.edges["disconnect"].status, "INSUFFICIENT_DATA")
        self.assertEqual(rep.overall_status, "INSUFFICIENT_DATA")


class TestReportFormats(unittest.TestCase):
    """Tests for text and JSON format representations."""

    def test_json_structure_and_no_pii(self) -> None:
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 10.0),
            HotplugRecord(2, "adb", 1, "disconnect", 10.5),
            HotplugRecord(3, "park", 1, "disconnect", 10.8),
            HotplugRecord(4, "marker", 1, "reconnect", 20.0),
            HotplugRecord(5, "adb", 1, "reconnect", 20.4),
            HotplugRecord(6, "park", 1, "reconnect", 20.3),  # park < adb
        ]
        rep = generate_report(records, criterion_min_samples=1)
        json_str = rep.to_json()
        data = json.loads(json_str)

        self.assertIn("status", data)
        self.assertIn("criterion", data)
        self.assertIn("summary", data)
        self.assertIn("transitions", data)
        self.assertIn("cycles", data)

        # Check disconnect and reconnect summaries
        disc = data["transitions"]["disconnect"]
        self.assertEqual(disc["valid_count"], 1)
        self.assertIn("metrics", disc)
        self.assertIn("marker_to_adb", disc["metrics"])
        self.assertIn("marker_to_park", disc["metrics"])
        self.assertIn("adb_to_park", disc["metrics"])

        # Check park < adb on reconnect
        recon_cycle = [c for c in data["cycles"] if c["edge"] == "reconnect"][0]
        self.assertEqual(recon_cycle["observation_order"], "park < adb")
        self.assertAlmostEqual(recon_cycle["adb_to_park_sec"], -0.1)

        # Check that no PII strings or unexpected keys exist
        raw_text = json.dumps(data)
        for forbidden in ("user", "operator_name", "email", "ip", "device_id", "serial"):
            self.assertNotIn(f'"{forbidden}"', raw_text)

    def test_text_report_contains_all_key_sections(self) -> None:
        records = [
            HotplugRecord(1, "marker", 1, "disconnect", 10.0),
            HotplugRecord(2, "adb", 1, "disconnect", 10.5),
            HotplugRecord(3, "park", 1, "disconnect", 10.8),
            HotplugRecord(4, "marker", 1, "reconnect", 20.0),
            HotplugRecord(5, "adb", 1, "reconnect", 20.7),
            HotplugRecord(6, "park", 1, "reconnect", 20.3),  # park < adb
        ]
        rep = generate_report(records, criterion_min_samples=1)
        text = rep.to_text()

        self.assertIn("DROIDHUB PHYSICAL HOTPLUG PILOT REPORT", text)
        self.assertIn("PER-CYCLE OBSERVATIONS", text)
        self.assertIn("DATA ANOMALIES & EXCLUSIONS SUMMARY", text)
        self.assertIn("AGGREGATE METRICS BY TRANSITION", text)
        self.assertIn("TRANSITION: DISCONNECT", text)
        self.assertIn("TRANSITION: RECONNECT", text)
        self.assertIn("PILOT ACCEPTANCE VERDICT", text)
        self.assertIn("[PARK FIRST]", text)


class TestCommandLineInterface(unittest.TestCase):
    """Tests for CLI invocation, error codes, and flags."""

    def test_cli_success_text(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            tf.write('{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0}\n')
            tf.write('{"kind": "adb", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.4}\n')
            tf.write('{"kind": "park", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.6}\n')
            temp_path = tf.name

        try:
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            exit_code = main(["--input", temp_path])
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

            self.assertEqual(exit_code, 0)
            self.assertIn("DROIDHUB PHYSICAL HOTPLUG PILOT REPORT", output)
        finally:
            os.remove(temp_path)

    def test_cli_success_json(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            tf.write('{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0}\n')
            tf.write('{"kind": "adb", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.4}\n')
            tf.write('{"kind": "park", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.6}\n')
            temp_path = tf.name

        try:
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            exit_code = main(["--input", temp_path, "--json"])
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

            self.assertEqual(exit_code, 0)
            data = json.loads(output)
            self.assertIn("status", data)
            self.assertIn("transitions", data)
        finally:
            os.remove(temp_path)

    def test_cli_malformed_syntax_error_exits_nonzero(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            tf.write('{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0}\n')
            tf.write('{"broken json\n')
            temp_path = tf.name

        try:
            old_stderr = sys.stderr
            sys.stderr = io.StringIO()
            exit_code = main(["--input", temp_path])
            err_output = sys.stderr.getvalue()
            sys.stderr = old_stderr

            self.assertNotEqual(exit_code, 0)
            self.assertIn("Line 2:", err_output)
            self.assertIn("syntax error", err_output)
        finally:
            os.remove(temp_path)

    def test_cli_schema_error_exits_nonzero(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            tf.write('{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0}\n')
            tf.write('{"kind": "unknown_kind", "cycle": 2, "edge": "disconnect", "elapsed_sec": 2.0}\n')
            temp_path = tf.name

        try:
            old_stderr = sys.stderr
            sys.stderr = io.StringIO()
            exit_code = main(["--input", temp_path])
            err_output = sys.stderr.getvalue()
            sys.stderr = old_stderr

            self.assertNotEqual(exit_code, 0)
            self.assertIn("Line 2:", err_output)
            self.assertIn("schema error", err_output)
        finally:
            os.remove(temp_path)

    def test_cli_missing_file_exits_nonzero(self) -> None:
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        exit_code = main(["--input", "/path/to/nonexistent_file_xyz.jsonl"])
        err_output = sys.stderr.getvalue()
        sys.stderr = old_stderr

        self.assertNotEqual(exit_code, 0)
        self.assertIn("file not found", err_output)

    def test_cli_via_subprocess(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            tf.write('{"kind": "marker", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.0}\n')
            tf.write('{"kind": "adb", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.4}\n')
            tf.write('{"kind": "park", "cycle": 1, "edge": "disconnect", "elapsed_sec": 1.6}\n')
            temp_path = tf.name

        try:
            proc = subprocess.run(
                [sys.executable, "scripts/desktop_usb_hotplug_report.py", "--input", temp_path, "--json"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            self.assertEqual(proc.returncode, 0)
            data = json.loads(proc.stdout)
            self.assertEqual(data["status"], "INSUFFICIENT_DATA")
        finally:
            os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
