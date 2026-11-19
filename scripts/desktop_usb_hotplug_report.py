#!/usr/bin/env python3
"""Autonomous offline report for DroidHub physical USB hotplug pilot.

Python stdlib only. No ADB, GUI, network, or physical device dependencies.
CLI:
    python scripts/desktop_usb_hotplug_report.py --input PATH [--json]
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


class HotplugError(Exception):
    """Base exception for parsing errors with line number."""

    def __init__(self, line_num: int, message: str) -> None:
        super().__init__(f"Line {line_num}: {message}")
        self.line_num = line_num
        self.message = message


class HotplugSyntaxError(HotplugError):
    """Raised on invalid JSON syntax or empty lines."""

    pass


class HotplugSchemaError(HotplugError):
    """Raised on schema violations (missing/invalid fields or types)."""

    pass


@dataclass(frozen=True)
class HotplugRecord:
    """Represents a single parsed and validated JSONL record."""

    line_num: int
    kind: str  # "marker", "adb", "park"
    cycle: int  # positive int > 0
    edge: str  # "disconnect", "reconnect"
    elapsed_sec: float  # finite non-negative float


def validate_record_dict(data: Any, line_num: int) -> HotplugRecord:
    """Validate JSON object against schema and return HotplugRecord."""
    if not isinstance(data, dict):
        raise HotplugSchemaError(
            line_num, f"schema error: expected JSON object, got {type(data).__name__}"
        )

    required_keys = {"kind", "cycle", "edge", "elapsed_sec"}
    actual_keys = set(data.keys())
    missing = required_keys - actual_keys
    if missing:
        raise HotplugSchemaError(
            line_num, f"schema error: missing required fields: {sorted(missing)}"
        )

    unexpected = actual_keys - required_keys
    if unexpected:
        raise HotplugSchemaError(
            line_num, f"schema error: unexpected fields: {sorted(unexpected)}"
        )

    kind = data["kind"]
    if kind not in ("marker", "adb", "park"):
        raise HotplugSchemaError(
            line_num,
            f"schema error: invalid kind '{kind}', must be marker, adb, or park",
        )

    edge = data["edge"]
    if edge not in ("disconnect", "reconnect"):
        raise HotplugSchemaError(
            line_num,
            f"schema error: invalid edge '{edge}', must be disconnect or reconnect",
        )

    cycle = data["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle <= 0:
        raise HotplugSchemaError(
            line_num,
            f"schema error: cycle must be a positive integer, got {cycle!r}",
        )

    elapsed = data["elapsed_sec"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise HotplugSchemaError(
            line_num,
            f"schema error: elapsed_sec must be a finite non-negative number, got {elapsed!r}",
        )

    return HotplugRecord(
        line_num=line_num,
        kind=kind,
        cycle=cycle,
        edge=edge,
        elapsed_sec=float(elapsed),
    )


def parse_line(line: str, line_num: int) -> HotplugRecord:
    """Parse and validate a single JSONL line."""
    stripped = line.strip()
    if not stripped:
        raise HotplugSyntaxError(line_num, "syntax error: empty line")

    try:
        data = json.loads(line)
    except json.JSONDecodeError as err:
        raise HotplugSyntaxError(
            line_num, f"syntax error: {err.msg} (col {err.colno})"
        ) from err

    return validate_record_dict(data, line_num)


def parse_jsonl_file(path: str) -> List[HotplugRecord]:
    """Parse all lines from a JSONL file."""
    records: List[HotplugRecord] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            records.append(parse_line(line, line_num))
    return records


def nearest_rank_percentile(values: List[float], p: float) -> float:
    """Compute P-th percentile using nearest rank method: rank = ceil(P / 100 * N)."""
    if not values:
        raise ValueError("Cannot calculate percentile of empty sequence")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    k = math.ceil((p / 100.0) * n)
    k = max(1, min(n, k))
    return sorted_vals[k - 1]


def compute_metrics(values: List[float]) -> Dict[str, Optional[float]]:
    """Compute p50, p95, and max for a sequence of values."""
    if not values:
        return {"p50": None, "p95": None, "max": None}
    return {
        "p50": round(nearest_rank_percentile(values, 50.0), 6),
        "p95": round(nearest_rank_percentile(values, 95.0), 6),
        "max": round(max(values), 6),
    }


@dataclass
class CycleEdgeDetail:
    """Detailed observation for a single cycle and edge."""

    cycle: int
    edge: str
    is_valid: bool
    marker_to_adb_sec: Optional[float] = None
    marker_to_park_sec: Optional[float] = None
    adb_to_park_sec: Optional[float] = None
    observation_order: Optional[str] = None  # "adb < park", "park < adb", "adb == park"
    order_note: Optional[str] = None
    anomalies: List[str] = field(default_factory=list)


@dataclass
class EdgeSummary:
    """Aggregated metrics and criterion outcome for a single transition edge."""

    edge: str
    status: str  # "PASSED", "FAILED", "INSUFFICIENT_DATA"
    criterion_met: Optional[bool]
    criterion_reason: str
    valid_count: int
    total_cycles_seen: int
    p95_marker_to_park_sec: Optional[float]
    metrics: Dict[str, Dict[str, Optional[float]]]
    observation_order_counts: Dict[str, int]
    anomalies_counts: Dict[str, int]
    anomaly_descriptions: List[str]


@dataclass
class ReportData:
    """Complete analyzed report data."""

    input_path: str
    total_records: int
    overall_status: str  # "PASSED", "FAILED", "INSUFFICIENT_DATA"
    criterion_threshold_sec: float
    criterion_min_samples: int
    cycles: List[CycleEdgeDetail]
    edges: Dict[str, EdgeSummary]
    anomalies_summary: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to JSON-serializable dictionary without PII."""
        return {
            "status": self.overall_status,
            "criterion": {
                "target_metric": f"p95 marker->park <= {self.criterion_threshold_sec}s",
                "threshold_sec": self.criterion_threshold_sec,
                "min_observations_per_edge": self.criterion_min_samples,
                "disconnect_status": self.edges["disconnect"].status,
                "reconnect_status": self.edges["reconnect"].status,
            },
            "summary": {
                "total_records_processed": self.total_records,
                "anomalies_total": self.anomalies_summary,
            },
            "transitions": {
                edge_name: {
                    "status": edge_info.status,
                    "criterion_met": edge_info.criterion_met,
                    "criterion_reason": edge_info.criterion_reason,
                    "valid_count": edge_info.valid_count,
                    "total_cycles_seen": edge_info.total_cycles_seen,
                    "p95_marker_to_park_sec": edge_info.p95_marker_to_park_sec,
                    "metrics": edge_info.metrics,
                    "observation_order": edge_info.observation_order_counts,
                    "anomalies_counts": edge_info.anomalies_counts,
                    "anomalies": edge_info.anomaly_descriptions,
                }
                for edge_name, edge_info in self.edges.items()
            },
            "cycles": [
                {
                    "cycle": c.cycle,
                    "edge": c.edge,
                    "valid": c.is_valid,
                    "marker_to_adb_sec": c.marker_to_adb_sec,
                    "marker_to_park_sec": c.marker_to_park_sec,
                    "adb_to_park_sec": c.adb_to_park_sec,
                    "observation_order": c.observation_order,
                    "order_note": c.order_note,
                    "anomalies": c.anomalies,
                }
                for c in self.cycles
            ],
        }

    def to_json(self) -> str:
        """Serialize report to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_text(self) -> str:
        """Format report into human-readable text."""
        lines: List[str] = []
        sep = "=" * 80
        thin_sep = "-" * 80

        lines.append(sep)
        lines.append("DROIDHUB PHYSICAL HOTPLUG PILOT REPORT")
        lines.append(sep)
        lines.append(f"Input file:        {self.input_path}")
        lines.append(f"Records processed: {self.total_records}")
        lines.append(f"Overall verdict:   {self.overall_status}")
        lines.append(
            f"Criterion:         p95 marker->park <= {self.criterion_threshold_sec}s "
            f"(requires >= {self.criterion_min_samples} valid observations per edge)"
        )
        lines.append("")

        lines.append(thin_sep)
        lines.append("PER-CYCLE OBSERVATIONS")
        lines.append(thin_sep)
        header = f"{'Cycle':<7}{'Edge':<13}{'marker->adb':<15}{'marker->park':<15}{'adb->park':<14}{'Status / Notes'}"
        lines.append(header)
        lines.append("-" * len(header))

        for c in self.cycles:
            if c.is_valid:
                m_adb = f"{c.marker_to_adb_sec:.4f}s" if c.marker_to_adb_sec is not None else "---"
                m_park = f"{c.marker_to_park_sec:.4f}s" if c.marker_to_park_sec is not None else "---"
                if c.adb_to_park_sec is not None:
                    if c.adb_to_park_sec > 0:
                        a_park = f"+{c.adb_to_park_sec:.4f}s"
                    elif c.adb_to_park_sec < 0:
                        a_park = f"{c.adb_to_park_sec:.4f}s"
                    else:
                        a_park = f" {c.adb_to_park_sec:.4f}s"
                else:
                    a_park = "---"

                notes = c.observation_order or "valid"
                if c.observation_order == "park < adb":
                    notes += " [PARK FIRST]"
                lines.append(
                    f"{c.cycle:<7}{c.edge:<13}{m_adb:<15}{m_park:<15}{a_park:<14}{notes}"
                )
            else:
                m_adb = "---"
                m_park = "---"
                a_park = "---"
                reason = "; ".join(c.anomalies) if c.anomalies else "invalid"
                lines.append(
                    f"{c.cycle:<7}{c.edge:<13}{m_adb:<15}{m_park:<15}{a_park:<14}EXCLUDED ({reason})"
                )

        lines.append("")
        lines.append(thin_sep)
        lines.append("DATA ANOMALIES & EXCLUSIONS SUMMARY")
        lines.append(thin_sep)
        lines.append(
            f"Total incomplete sets:           {self.anomalies_summary['incomplete_sets']}"
        )
        lines.append(
            f"Total duplicates:                {self.anomalies_summary['duplicates']}"
        )
        lines.append(
            f"Total observations before marker:{self.anomalies_summary['observations_before_marker']}"
        )

        all_anomalies: List[str] = []
        for edge_info in self.edges.values():
            all_anomalies.extend(edge_info.anomaly_descriptions)
        if all_anomalies:
            lines.append("Excluded items detail:")
            for desc in all_anomalies:
                lines.append(f"  - {desc}")
        else:
            lines.append("No anomalies detected; all cycles complete and valid.")

        lines.append("")
        lines.append(thin_sep)
        lines.append("AGGREGATE METRICS BY TRANSITION")
        lines.append(thin_sep)

        for edge_name in ("disconnect", "reconnect"):
            edge_info = self.edges[edge_name]
            lines.append(f"TRANSITION: {edge_name.upper()}")
            lines.append(
                f"  Valid complete observations: {edge_info.valid_count} (Total seen: {edge_info.total_cycles_seen})"
            )
            m = edge_info.metrics
            for metric_key, metric_name in (
                ("marker_to_adb", "marker->adb"),
                ("marker_to_park", "marker->park"),
                ("adb_to_park", "adb->park"),
            ):
                p50 = f"{m[metric_key]['p50']:.4f}s" if m[metric_key]["p50"] is not None else "N/A"
                p95 = f"{m[metric_key]['p95']:.4f}s" if m[metric_key]["p95"] is not None else "N/A"
                max_v = f"{m[metric_key]['max']:.4f}s" if m[metric_key]["max"] is not None else "N/A"
                lines.append(
                    f"  {metric_name:<14} p50 = {p50:<10} | p95 = {p95:<10} | max = {max_v}"
                )

            orders = edge_info.observation_order_counts
            lines.append("  Observation order breakdown:")
            lines.append(
                f"    adb < park (adb first):     {orders.get('adb_first', 0)}"
            )
            lines.append(
                f"    park < adb (park first):    {orders.get('park_first', 0)} (negative adb->park diff, timestamps >= marker)"
            )
            lines.append(
                f"    adb == park (simultaneous): {orders.get('simultaneous', 0)}"
            )

            lines.append(
                f"  Criterion evaluation: {edge_info.status} ({edge_info.criterion_reason})"
            )
            lines.append("")

        lines.append(sep)
        lines.append(f"PILOT ACCEPTANCE VERDICT: {self.overall_status}")
        for edge_name in ("disconnect", "reconnect"):
            e = self.edges[edge_name]
            p95_str = (
                f"{e.p95_marker_to_park_sec:.4f}s"
                if e.p95_marker_to_park_sec is not None
                else "N/A"
            )
            lines.append(
                f"  {edge_name.capitalize():<12}: {e.status:<18} (valid N={e.valid_count}, p95={p95_str})"
            )
        lines.append(sep)
        return "\n".join(lines)


def generate_report(
    records: List[HotplugRecord],
    input_path: str = "<memory>",
    criterion_threshold_sec: float = 4.0,
    criterion_min_samples: int = 30,
) -> ReportData:
    """Analyze records and generate full ReportData."""
    # Group records by (cycle, edge)
    records_by_cycle_edge: Dict[Tuple[int, str], Dict[str, List[HotplugRecord]]] = {}

    all_cycles_seen = set()
    edges_present = set()
    for r in records:
        all_cycles_seen.add(r.cycle)
        edges_present.add(r.edge)
        key = (r.cycle, r.edge)
        if key not in records_by_cycle_edge:
            records_by_cycle_edge[key] = {"marker": [], "adb": [], "park": []}
        records_by_cycle_edge[key][r.kind].append(r)

    # If there are cycles, ensure we check all cycles in range(1, max_cycle + 1)
    # for each edge present in the records to avoid masking gaps/omitted cycles.
    if all_cycles_seen:
        max_cycle = max(all_cycles_seen)
        cycle_range = list(range(1, max_cycle + 1))
    else:
        cycle_range = []

    cycle_details: List[CycleEdgeDetail] = []
    edges_details: Dict[str, List[CycleEdgeDetail]] = {
        "disconnect": [],
        "reconnect": [],
    }

    for cycle in cycle_range:
        for edge in ("disconnect", "reconnect"):
            if edge not in edges_present:
                continue

            key = (cycle, edge)
            kind_recs = records_by_cycle_edge.get(
                key, {"marker": [], "adb": [], "park": []}
            )

            anomalies: List[str] = []

            # 1. Check duplicates
            dup_kinds = [k for k in ("marker", "adb", "park") if len(kind_recs[k]) > 1]
            for k in dup_kinds:
                lines = [r.line_num for r in kind_recs[k]]
                anomalies.append(
                    f"duplicate {k} ({len(kind_recs[k])} occurrences on lines {lines})"
                )

            # 2. Check incomplete set
            missing_kinds = [
                k for k in ("marker", "adb", "park") if len(kind_recs[k]) == 0
            ]
            if missing_kinds:
                present_kinds = [
                    k for k in ("marker", "adb", "park") if len(kind_recs[k]) > 0
                ]
                if not present_kinds:
                    anomalies.append("incomplete set: cycle transition missing entirely")
                else:
                    anomalies.append(
                        f"incomplete set: missing {', '.join(missing_kinds)} (present: {', '.join(present_kinds)})"
                    )

            # 3. Check observation before marker (only if marker exists)
            if len(kind_recs["marker"]) == 1:
                t_marker = kind_recs["marker"][0].elapsed_sec
                for r in kind_recs["adb"]:
                    if r.elapsed_sec < t_marker:
                        anomalies.append(
                            f"observation before marker: adb ({r.elapsed_sec:.4f}s) < marker ({t_marker:.4f}s) [line {r.line_num}]"
                        )
                for r in kind_recs["park"]:
                    if r.elapsed_sec < t_marker:
                        anomalies.append(
                            f"observation before marker: park ({r.elapsed_sec:.4f}s) < marker ({t_marker:.4f}s) [line {r.line_num}]"
                        )

            is_valid = len(anomalies) == 0
            m_adb: Optional[float] = None
            m_park: Optional[float] = None
            a_park: Optional[float] = None
            order: Optional[str] = None
            order_note: Optional[str] = None

            if is_valid:
                t_marker = kind_recs["marker"][0].elapsed_sec
                t_adb = kind_recs["adb"][0].elapsed_sec
                t_park = kind_recs["park"][0].elapsed_sec

                m_adb = round(t_adb - t_marker, 6)
                m_park = round(t_park - t_marker, 6)
                a_park = round(t_park - t_adb, 6)

                if t_park < t_adb:
                    order = "park < adb"
                    order_note = (
                        "park observed before adb (negative adb->park diff, timestamps >= marker)"
                    )
                elif t_adb < t_park:
                    order = "adb < park"
                    order_note = "adb observed before park"
                else:
                    order = "adb == park"
                    order_note = "simultaneous observations"

            detail = CycleEdgeDetail(
                cycle=cycle,
                edge=edge,
                is_valid=is_valid,
                marker_to_adb_sec=m_adb,
                marker_to_park_sec=m_park,
                adb_to_park_sec=a_park,
                observation_order=order,
                order_note=order_note,
                anomalies=anomalies,
            )
            cycle_details.append(detail)
            edges_details[edge].append(detail)

    # Aggregate summaries by edge
    edge_summaries: Dict[str, EdgeSummary] = {}
    total_incomplete = 0
    total_duplicates = 0
    total_before_marker = 0

    for edge in ("disconnect", "reconnect"):
        details = edges_details[edge]
        valid_details = [d for d in details if d.is_valid]
        valid_count = len(valid_details)
        total_seen = len(details)

        m_adb_list = [
            d.marker_to_adb_sec
            for d in valid_details
            if d.marker_to_adb_sec is not None
        ]
        m_park_list = [
            d.marker_to_park_sec
            for d in valid_details
            if d.marker_to_park_sec is not None
        ]
        a_park_list = [
            d.adb_to_park_sec for d in valid_details if d.adb_to_park_sec is not None
        ]

        metrics = {
            "marker_to_adb": compute_metrics(m_adb_list),
            "marker_to_park": compute_metrics(m_park_list),
            "adb_to_park": compute_metrics(a_park_list),
        }

        order_counts = {
            "adb_first": sum(1 for d in valid_details if d.observation_order == "adb < park"),
            "park_first": sum(1 for d in valid_details if d.observation_order == "park < adb"),
            "simultaneous": sum(1 for d in valid_details if d.observation_order == "adb == park"),
        }

        # Count anomalies for this edge
        inc_count = sum(
            1 for d in details if any("incomplete set" in a for a in d.anomalies)
        )
        dup_count = sum(
            1 for d in details if any("duplicate" in a for a in d.anomalies)
        )
        before_count = sum(
            1 for d in details if any("observation before marker" in a for a in d.anomalies)
        )

        total_incomplete += inc_count
        total_duplicates += dup_count
        total_before_marker += before_count

        anomaly_descriptions: List[str] = []
        for d in details:
            for anom in d.anomalies:
                anomaly_descriptions.append(f"Cycle {d.cycle} {d.edge}: {anom}")

        p95_park = metrics["marker_to_park"]["p95"]

        # Evaluate criterion for this edge
        if valid_count < criterion_min_samples:
            status = "INSUFFICIENT_DATA"
            criterion_met = None
            reason = f"insufficient data: {valid_count} valid complete observations (< {criterion_min_samples} required)"
        else:
            assert p95_park is not None
            if p95_park <= criterion_threshold_sec:
                status = "PASSED"
                criterion_met = True
                reason = f"p95 {p95_park:.4f}s <= {criterion_threshold_sec}s with {valid_count} valid observations"
            else:
                status = "FAILED"
                criterion_met = False
                reason = f"p95 {p95_park:.4f}s > {criterion_threshold_sec}s with {valid_count} valid observations"

        edge_summaries[edge] = EdgeSummary(
            edge=edge,
            status=status,
            criterion_met=criterion_met,
            criterion_reason=reason,
            valid_count=valid_count,
            total_cycles_seen=total_seen,
            p95_marker_to_park_sec=p95_park,
            metrics=metrics,
            observation_order_counts=order_counts,
            anomalies_counts={
                "incomplete": inc_count,
                "duplicates": dup_count,
                "before_marker": before_count,
            },
            anomaly_descriptions=anomaly_descriptions,
        )

    # Evaluate overall status
    if (
        edge_summaries["disconnect"].status == "INSUFFICIENT_DATA"
        or edge_summaries["reconnect"].status == "INSUFFICIENT_DATA"
    ):
        overall_status = "INSUFFICIENT_DATA"
    elif (
        edge_summaries["disconnect"].status == "PASSED"
        and edge_summaries["reconnect"].status == "PASSED"
    ):
        overall_status = "PASSED"
    else:
        overall_status = "FAILED"

    return ReportData(
        input_path=input_path,
        total_records=len(records),
        overall_status=overall_status,
        criterion_threshold_sec=criterion_threshold_sec,
        criterion_min_samples=criterion_min_samples,
        cycles=cycle_details,
        edges=edge_summaries,
        anomalies_summary={
            "incomplete_sets": total_incomplete,
            "duplicates": total_duplicates,
            "observations_before_marker": total_before_marker,
        },
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Autonomous offline physical hotplug pilot report for DroidHub"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to input JSONL file containing hotplug pilot records",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report in JSON format",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        sys.stderr.write(f"Error: input file not found: {args.input}\n")
        return 1

    try:
        records = parse_jsonl_file(args.input)
    except HotplugError as err:
        sys.stderr.write(f"Line {err.line_num}: {err.message}\n")
        return 1

    report = generate_report(records, input_path=args.input)

    if args.json:
        sys.stdout.write(report.to_json() + "\n")
    else:
        sys.stdout.write(report.to_text() + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
