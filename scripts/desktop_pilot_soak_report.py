#!/usr/bin/env python3
"""
scripts/desktop_pilot_soak_report.py - Анализ локального JSONL длительного пилотного прогона.

Standalone stdlib Python скрипт (без сторонних зависимостей).
Не зависит от desktop/, ничего не запускает, ничего не меняет на телефонах и в приложении.

Минимальный формат JSONL:
    {"elapsed_sec": 0.0, "type": "start"}
    {"elapsed_sec": 600.0, "type": "sample", "rss_mb": 150.0}
    {"elapsed_sec": 650.0, "type": "run", "status": "succeeded"}
    {"elapsed_sec": 700.0, "type": "disconnect"}
    {"elapsed_sec": 720.0, "type": "reconnect"}
    {"elapsed_sec": 28800.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0}

Пример вызова:
    python scripts/desktop_pilot_soak_report.py /path/to/soak.jsonl --warmup-sec 600 --json
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence


class SchemaError(ValueError):
    """Ошибка валидации схемы входного JSONL файла."""
    pass


# Пороги пилотной приёмки
THRESHOLD_DURATION_SEC = 28800.0  # 8 часов
THRESHOLD_TOTAL_RUNS = 100
THRESHOLD_FULL_CYCLES = 20
THRESHOLD_RSS_GROWTH_PCT = 20.0
THRESHOLD_CRASHES = 0
THRESHOLD_LOST_RUNS = 0
THRESHOLD_REMAINING_CHILDREN = 0

DEFAULT_WARMUP_SEC = 600.0
RSS_FORMULA_STR = "((max_rss_mb - first_rss_mb) / first_rss_mb) * 100"

ALLOWED_TYPES = frozenset({"start", "sample", "run", "disconnect", "reconnect", "shutdown"})
ALLOWED_RUN_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})

MINIMAL_JSONL_FORMAT: list[dict[str, Any]] = [
    {"elapsed_sec": 0.0, "type": "start"},
    {"elapsed_sec": 600.0, "type": "sample", "rss_mb": 150.0},
    {"elapsed_sec": 650.0, "type": "run", "status": "succeeded"},
    {"elapsed_sec": 700.0, "type": "disconnect"},
    {"elapsed_sec": 720.0, "type": "reconnect"},
    {"elapsed_sec": 28800.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
]

EXAMPLE_INVOCATION = "python scripts/desktop_pilot_soak_report.py soak.jsonl --warmup-sec 600 --json"


@dataclass
class InputCompleteness:
    records_count: int = 0
    has_start: bool = False
    has_shutdown: bool = False
    is_shutdown_final: bool = False
    is_complete: bool = False


@dataclass
class DurationMetrics:
    duration_sec: float = 0.0
    duration_hours: float = 0.0
    threshold_sec: float = THRESHOLD_DURATION_SEC
    threshold_passed: bool = False


@dataclass
class RunMetrics:
    total_runs: int = 0
    succeeded_runs: int = 0
    partial_runs: int = 0
    failed_runs: int = 0
    cancelled_runs: int = 0
    success_rate_pct: float = 0.0
    threshold_runs: int = THRESHOLD_TOTAL_RUNS
    threshold_passed: bool = False


@dataclass
class CycleMetrics:
    full_cycles: int = 0
    disconnect_count: int = 0
    reconnect_count: int = 0
    unpaired_disconnects: int = 0
    unpaired_reconnects: int = 0
    threshold_cycles: int = THRESHOLD_FULL_CYCLES
    threshold_passed: bool = False


@dataclass
class RssMetrics:
    warmup_sec: float = DEFAULT_WARMUP_SEC
    post_warmup_samples_count: int = 0
    first_rss_mb: float | None = None
    max_rss_mb: float | None = None
    growth_pct: float | None = None
    formula: str = RSS_FORMULA_STR
    threshold_pct: float = THRESHOLD_RSS_GROWTH_PCT
    threshold_passed: bool | None = None


@dataclass
class ShutdownMetrics:
    crashes: int | None = None
    lost_runs: int | None = None
    remaining_children: int | None = None
    zero_crashes_passed: bool | None = None
    zero_lost_runs_passed: bool | None = None
    zero_remaining_children_passed: bool | None = None


@dataclass
class ThresholdsSummary:
    duration_passed: bool = False
    runs_passed: bool = False
    cycles_passed: bool = False
    rss_passed: bool = False
    crashes_passed: bool = False
    lost_runs_passed: bool = False
    remaining_children_passed: bool = False
    all_thresholds_passed: bool = False


@dataclass
class SoakReport:
    status: str  # "pass", "fail", "incomplete"
    input_completeness: InputCompleteness = field(default_factory=InputCompleteness)
    duration: DurationMetrics = field(default_factory=DurationMetrics)
    runs: RunMetrics = field(default_factory=RunMetrics)
    cycles: CycleMetrics = field(default_factory=CycleMetrics)
    rss: RssMetrics = field(default_factory=RssMetrics)
    shutdown: ShutdownMetrics = field(default_factory=ShutdownMetrics)
    thresholds_summary: ThresholdsSummary = field(default_factory=ThresholdsSummary)
    reasons: list[str] = field(default_factory=list)
    minimal_jsonl_format: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(item) for item in MINIMAL_JSONL_FORMAT]
    )
    example_invocation: str = EXAMPLE_INVOCATION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_text(self) -> str:
        status_label = self.status.upper()
        lines: list[str] = [
            "=" * 80,
            "ОТЧЁТ О ДЛИТЕЛЬНОМ ПИЛОТНОМ ПРОГОНЕ (SOAK TEST REPORT)",
            "=" * 80,
            f"ИТОГ: {status_label}",
            "",
            "1. ПОЛНОТА ВХОДА:",
            f"   - Всего записей: {self.input_completeness.records_count}",
            f"   - Наличие start: {'ДА' if self.input_completeness.has_start else 'НЕТ'}",
            f"   - Наличие shutdown: {'ДА' if self.input_completeness.has_shutdown else 'НЕТ'} "
            f"(финальная запись: {'ДА' if self.input_completeness.is_shutdown_final else 'НЕТ'})",
            f"   - Полнота входа: {'ПОЛНЫЙ' if self.input_completeness.is_complete else 'НЕПОЛНЫЙ'}",
            "",
            "2. ДЛИТЕЛЬНОСТЬ:",
            f"   - Длительность: {self.duration.duration_sec:.1f} с ({self.duration.duration_hours:.2f} ч)",
            f"   - Порог пилота: >= {self.duration.threshold_sec:.1f} с (8.0 ч) -> "
            f"{'ВЫПОЛНЕН' if self.duration.threshold_passed else 'НЕ ВЫПОЛНЕН'}",
            "",
            "3. ЗАПУСКИ (RUNS):",
            f"   - Число завершённых запусков: {self.runs.total_runs} (порог: >= {self.runs.threshold_runs}) -> "
            f"{'ВЫПОЛНЕН' if self.runs.threshold_passed else 'НЕ ВЫПОЛНЕН'}",
            "   - Счётчик успешности:",
            f"     * Успешные (succeeded): {self.runs.succeeded_runs} ({self.runs.success_rate_pct:.1f}%)",
            f"     * Частичные (partial):   {self.runs.partial_runs}",
            f"     * Неудачные (failed):    {self.runs.failed_runs}",
            f"     * Отменённые (cancelled): {self.runs.cancelled_runs}",
            "",
            "4. ЦИКЛЫ ПОДКЛЮЧЕНИЯ (DISCONNECT / RECONNECT):",
            f"   - Полных пар (циклов): {self.cycles.full_cycles} (порог: >= {self.cycles.threshold_cycles}) -> "
            f"{'ВЫПОЛНЕН' if self.cycles.threshold_passed else 'НЕ ВЫПОЛНЕН'}",
            f"   - Всего disconnect: {self.cycles.disconnect_count}, reconnect: {self.cycles.reconnect_count}",
            f"   - Непарных disconnect: {self.cycles.unpaired_disconnects}, reconnect: {self.cycles.unpaired_reconnects}",
            "",
            "5. ПАМЯТЬ RSS (ПОСЛЕ ПРОГРЕВА):",
            f"   - Длительность прогрева: {self.rss.warmup_sec:.1f} с",
            f"   - Измерений после прогрева: {self.rss.post_warmup_samples_count}",
        ]

        if self.rss.first_rss_mb is not None and self.rss.max_rss_mb is not None:
            lines.extend([
                f"   - Первый RSS после прогрева: {self.rss.first_rss_mb:.2f} МБ",
                f"   - Максимальный RSS после прогрева: {self.rss.max_rss_mb:.2f} МБ",
                f"   - Формула: {self.rss.formula}",
                f"   - Рост RSS: {self.rss.growth_pct:.2f}% (порог: <= {self.rss.threshold_pct:.1f}%) -> "
                f"{'ВЫПОЛНЕН' if self.rss.threshold_passed else 'НЕ ВЫПОЛНЕН'}",
            ])
        else:
            lines.append("   - Измерения RSS после прогрева отсутствуют")

        lines.extend([
            "",
            "6. МЕТРИКИ SHUTDOWN:",
        ])
        if self.shutdown.crashes is not None:
            lines.extend([
                f"   - Crashes: {self.shutdown.crashes} (порог: 0) -> "
                f"{'ВЫПОЛНЕН' if self.shutdown.zero_crashes_passed else 'НЕ ВЫПОЛНЕН'}",
                f"   - Lost runs: {self.shutdown.lost_runs} (порог: 0) -> "
                f"{'ВЫПОЛНЕН' if self.shutdown.zero_lost_runs_passed else 'НЕ ВЫПОЛНЕН'}",
                f"   - Remaining children: {self.shutdown.remaining_children} (порог: 0) -> "
                f"{'ВЫПОЛНЕН' if self.shutdown.zero_remaining_children_passed else 'НЕ ВЫПОЛНЕН'}",
            ])
        else:
            lines.append("   - Запись shutdown отсутствует")

        if self.reasons:
            lines.extend([
                "",
                "7. ЗАМЕЧАНИЯ И ПРИЧИНЫ:",
            ])
            for r in self.reasons:
                lines.append(f"   * {r}")

        lines.extend([
            "",
            "-" * 80,
            "МИНИМАЛЬНЫЙ ФОРМАТ JSONL:",
        ])
        for row in self.minimal_jsonl_format:
            lines.append(f"  {json.dumps(row, ensure_ascii=False)}")
        lines.extend([
            "",
            "ПРИМЕР ВЫЗОВА:",
            f"  {self.example_invocation}",
            "=" * 80,
        ])
        return "\n".join(lines)


def parse_soak_records(
    lines: Iterable[str],
    *,
    warmup_sec: float = DEFAULT_WARMUP_SEC,
) -> SoakReport:
    """
    Разбирает и валидирует строки JSONL длительного пилотного прогона.

    Вычисляет все требуемые метрики и возвращает SoakReport.
    При нарушении формата/схемы возбуждает SchemaError.
    """
    if warmup_sec < 0:
        raise SchemaError(f"Время прогрева (warmup_sec) не может быть отрицательным: {warmup_sec}")

    records_count = 0
    has_start = False
    start_elapsed_sec: float | None = None
    has_shutdown = False
    shutdown_elapsed_sec: float | None = None
    is_shutdown_final = False

    prev_elapsed_sec: float | None = None

    samples: list[tuple[float, float]] = []  # (elapsed_sec, rss_mb)
    run_statuses: list[str] = []

    disconnect_count = 0
    reconnect_count = 0
    pending_disconnects = 0
    full_cycles = 0
    unpaired_reconnects = 0

    crashes: int | None = None
    lost_runs: int | None = None
    remaining_children: int | None = None

    for line_idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        try:
            record = json.loads(line)
        except Exception as e:
            raise SchemaError(f"Строка {line_idx}: Невалидный JSON: {e}") from e

        if not isinstance(record, dict):
            raise SchemaError(
                f"Строка {line_idx}: Запись должна быть JSON-объектом, получено {type(record).__name__}"
            )

        # Валидация elapsed_sec
        if "elapsed_sec" not in record:
            raise SchemaError(f"Строка {line_idx}: Отсутствует обязательное поле 'elapsed_sec'")

        elapsed = record["elapsed_sec"]
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            raise SchemaError(
                f"Строка {line_idx}: Поле 'elapsed_sec' должно быть числом, получено {type(elapsed).__name__}"
            )
        if not math.isfinite(elapsed):
            raise SchemaError(f"Строка {line_idx}: Поле 'elapsed_sec' должно быть конечным числом")
        if elapsed < 0:
            raise SchemaError(
                f"Строка {line_idx}: Поле 'elapsed_sec' должно быть неотрицательным, получено {elapsed}"
            )

        elapsed_val = float(elapsed)
        if prev_elapsed_sec is not None and elapsed_val < prev_elapsed_sec:
            raise SchemaError(
                f"Строка {line_idx}: Нарушение монотонности: elapsed_sec ({elapsed_val}) "
                f"меньше предыдущего ({prev_elapsed_sec})"
            )
        prev_elapsed_sec = elapsed_val

        # Валидация type
        if "type" not in record:
            raise SchemaError(f"Строка {line_idx}: Отсутствует обязательное поле 'type'")

        rec_type = record["type"]
        if not isinstance(rec_type, str) or rec_type not in ALLOWED_TYPES:
            raise SchemaError(
                f"Строка {line_idx}: Недопустимый type '{rec_type}', ожидается один из {sorted(ALLOWED_TYPES)}"
            )

        # shutdown должен быть строго финальным: записи после него недопустимы
        if has_shutdown:
            raise SchemaError(
                f"Строка {line_idx}: Запись '{rec_type}' после shutdown недопустима; "
                f"shutdown должен быть финальной записью"
            )

        records_count += 1

        if rec_type == "start":
            if has_start:
                raise SchemaError(f"Строка {line_idx}: Повторная запись 'start'")
            has_start = True
            start_elapsed_sec = elapsed_val

        elif rec_type == "shutdown":
            has_shutdown = True
            is_shutdown_final = True
            shutdown_elapsed_sec = elapsed_val

            for field_name in ("crashes", "lost_runs", "remaining_children"):
                if field_name not in record:
                    raise SchemaError(
                        f"Строка {line_idx}: В shutdown отсутствует обязательное поле '{field_name}'"
                    )
                val = record[field_name]
                if isinstance(val, bool) or not isinstance(val, int):
                    raise SchemaError(
                        f"Строка {line_idx}: Поле '{field_name}' в shutdown должно быть целым числом, "
                        f"получено {type(val).__name__}"
                    )
                if val < 0:
                    raise SchemaError(
                        f"Строка {line_idx}: Поле '{field_name}' в shutdown должно быть неотрицательным, "
                        f"получено {val}"
                    )

            crashes = record["crashes"]
            lost_runs = record["lost_runs"]
            remaining_children = record["remaining_children"]

        elif rec_type == "sample":
            if "rss_mb" not in record:
                raise SchemaError(f"Строка {line_idx}: В sample отсутствует обязательное поле 'rss_mb'")
            rss = record["rss_mb"]
            if isinstance(rss, bool) or not isinstance(rss, (int, float)):
                raise SchemaError(
                    f"Строка {line_idx}: Поле 'rss_mb' должно быть числом, получено {type(rss).__name__}"
                )
            if not math.isfinite(rss):
                raise SchemaError(f"Строка {line_idx}: Поле 'rss_mb' должно быть конечным числом")
            if rss <= 0:
                raise SchemaError(
                    f"Строка {line_idx}: Поле 'rss_mb' должно быть положительным числом (> 0), получено {rss}"
                )
            samples.append((elapsed_val, float(rss)))

        elif rec_type == "run":
            if "status" not in record:
                raise SchemaError(f"Строка {line_idx}: В run отсутствует обязательное поле 'status'")
            status_val = record["status"]
            if not isinstance(status_val, str) or status_val not in ALLOWED_RUN_STATUSES:
                raise SchemaError(
                    f"Строка {line_idx}: Недопустимый status '{status_val}' в run, "
                    f"ожидается один из {sorted(ALLOWED_RUN_STATUSES)}"
                )
            run_statuses.append(status_val)

        elif rec_type == "disconnect":
            disconnect_count += 1
            pending_disconnects += 1

        elif rec_type == "reconnect":
            reconnect_count += 1
            if pending_disconnects > 0:
                pending_disconnects -= 1
                full_cycles += 1
            else:
                unpaired_reconnects += 1

    unpaired_disconnects = pending_disconnects
    is_complete = has_start and has_shutdown and is_shutdown_final

    input_completeness = InputCompleteness(
        records_count=records_count,
        has_start=has_start,
        has_shutdown=has_shutdown,
        is_shutdown_final=is_shutdown_final,
        is_complete=is_complete,
    )

    # Расчёт длительности
    if has_start and start_elapsed_sec is not None:
        if has_shutdown and shutdown_elapsed_sec is not None:
            duration_sec = max(0.0, shutdown_elapsed_sec - start_elapsed_sec)
        elif prev_elapsed_sec is not None:
            duration_sec = max(0.0, prev_elapsed_sec - start_elapsed_sec)
        else:
            duration_sec = 0.0
    elif prev_elapsed_sec is not None:
        duration_sec = prev_elapsed_sec
    else:
        duration_sec = 0.0

    duration_hours = duration_sec / 3600.0
    duration_passed = duration_sec >= THRESHOLD_DURATION_SEC

    duration_metrics = DurationMetrics(
        duration_sec=duration_sec,
        duration_hours=duration_hours,
        threshold_sec=THRESHOLD_DURATION_SEC,
        threshold_passed=duration_passed,
    )

    # Расчёт метрик запусков
    total_runs = len(run_statuses)
    succeeded_runs = run_statuses.count("succeeded")
    partial_runs = run_statuses.count("partial")
    failed_runs = run_statuses.count("failed")
    cancelled_runs = run_statuses.count("cancelled")
    success_rate_pct = (succeeded_runs / total_runs * 100.0) if total_runs > 0 else 0.0
    runs_passed = total_runs >= THRESHOLD_TOTAL_RUNS

    run_metrics = RunMetrics(
        total_runs=total_runs,
        succeeded_runs=succeeded_runs,
        partial_runs=partial_runs,
        failed_runs=failed_runs,
        cancelled_runs=cancelled_runs,
        success_rate_pct=success_rate_pct,
        threshold_runs=THRESHOLD_TOTAL_RUNS,
        threshold_passed=runs_passed,
    )

    # Расчёт циклов
    cycles_passed = full_cycles >= THRESHOLD_FULL_CYCLES
    cycle_metrics = CycleMetrics(
        full_cycles=full_cycles,
        disconnect_count=disconnect_count,
        reconnect_count=reconnect_count,
        unpaired_disconnects=unpaired_disconnects,
        unpaired_reconnects=unpaired_reconnects,
        threshold_cycles=THRESHOLD_FULL_CYCLES,
        threshold_passed=cycles_passed,
    )

    # Расчёт RSS после прогрева
    post_warmup_samples = [s for s in samples if s[0] >= warmup_sec]
    if post_warmup_samples:
        first_rss_mb = post_warmup_samples[0][1]
        max_rss_mb = max(s[1] for s in post_warmup_samples)
        rss_growth_pct = ((max_rss_mb - first_rss_mb) / first_rss_mb) * 100.0
        rss_passed = rss_growth_pct <= THRESHOLD_RSS_GROWTH_PCT
    else:
        first_rss_mb = None
        max_rss_mb = None
        rss_growth_pct = None
        rss_passed = None

    rss_metrics = RssMetrics(
        warmup_sec=warmup_sec,
        post_warmup_samples_count=len(post_warmup_samples),
        first_rss_mb=first_rss_mb,
        max_rss_mb=max_rss_mb,
        growth_pct=rss_growth_pct,
        formula=RSS_FORMULA_STR,
        threshold_pct=THRESHOLD_RSS_GROWTH_PCT,
        threshold_passed=rss_passed,
    )

    # Метрики shutdown
    zero_crashes_passed = (crashes == THRESHOLD_CRASHES) if crashes is not None else None
    zero_lost_runs_passed = (lost_runs == THRESHOLD_LOST_RUNS) if lost_runs is not None else None
    zero_remaining_children_passed = (
        (remaining_children == THRESHOLD_REMAINING_CHILDREN) if remaining_children is not None else None
    )

    shutdown_metrics = ShutdownMetrics(
        crashes=crashes,
        lost_runs=lost_runs,
        remaining_children=remaining_children,
        zero_crashes_passed=zero_crashes_passed,
        zero_lost_runs_passed=zero_lost_runs_passed,
        zero_remaining_children_passed=zero_remaining_children_passed,
    )

    # Оценка соответствия порогам и определение статуса
    defect_violations: list[str] = []
    if crashes is not None and crashes > THRESHOLD_CRASHES:
        defect_violations.append(f"Число падений (crashes): {crashes} > {THRESHOLD_CRASHES}")
    if lost_runs is not None and lost_runs > THRESHOLD_LOST_RUNS:
        defect_violations.append(f"Число потерянных запусков (lost_runs): {lost_runs} > {THRESHOLD_LOST_RUNS}")
    if remaining_children is not None and remaining_children > THRESHOLD_REMAINING_CHILDREN:
        defect_violations.append(
            f"Число оставшихся дочерних процессов (remaining_children): {remaining_children} > {THRESHOLD_REMAINING_CHILDREN}"
        )
    if rss_growth_pct is not None and rss_growth_pct > THRESHOLD_RSS_GROWTH_PCT:
        defect_violations.append(
            f"Рост RSS превысил порог: {rss_growth_pct:.2f}% > {THRESHOLD_RSS_GROWTH_PCT}%"
        )

    measurement_issues: list[str] = []
    if not has_start:
        measurement_issues.append("Отсутствует запись 'start'")
    if not has_shutdown:
        measurement_issues.append("Отсутствует запись 'shutdown'")
    if not post_warmup_samples:
        measurement_issues.append(f"Нет измерений RSS после прогрева ({warmup_sec:.1f} с)")
    if duration_sec < THRESHOLD_DURATION_SEC:
        measurement_issues.append(
            f"Длительность недостаточна: {duration_sec:.1f} с ({duration_hours:.2f} ч) < {THRESHOLD_DURATION_SEC:.1f} с (8.0 ч)"
        )
    if total_runs < THRESHOLD_TOTAL_RUNS:
        measurement_issues.append(
            f"Число завершённых запусков недостаточно: {total_runs} < {THRESHOLD_TOTAL_RUNS}"
        )
    if full_cycles < THRESHOLD_FULL_CYCLES:
        measurement_issues.append(
            f"Число полных циклов disconnect/reconnect недостаточно: {full_cycles} < {THRESHOLD_FULL_CYCLES}"
        )

    if defect_violations:
        status = "fail"
        reasons = defect_violations + measurement_issues
    elif measurement_issues:
        status = "incomplete"
        reasons = measurement_issues
    else:
        status = "pass"
        reasons = []

    crashes_ok = zero_crashes_passed is True
    lost_runs_ok = zero_lost_runs_passed is True
    remaining_children_ok = zero_remaining_children_passed is True
    rss_ok = rss_passed is True

    all_thresholds_passed = (
        duration_passed
        and runs_passed
        and cycles_passed
        and rss_ok
        and crashes_ok
        and lost_runs_ok
        and remaining_children_ok
        and is_complete
    )

    thresholds_summary = ThresholdsSummary(
        duration_passed=duration_passed,
        runs_passed=runs_passed,
        cycles_passed=cycles_passed,
        rss_passed=rss_ok,
        crashes_passed=crashes_ok,
        lost_runs_passed=lost_runs_ok,
        remaining_children_passed=remaining_children_ok,
        all_thresholds_passed=all_thresholds_passed,
    )

    return SoakReport(
        status=status,
        input_completeness=input_completeness,
        duration=duration_metrics,
        runs=run_metrics,
        cycles=cycle_metrics,
        rss=rss_metrics,
        shutdown=shutdown_metrics,
        thresholds_summary=thresholds_summary,
        reasons=reasons,
    )


def parse_soak_file(file_path: str | Path, *, warmup_sec: float = DEFAULT_WARMUP_SEC) -> SoakReport:
    """Читает локальный JSONL файл и строит SoakReport."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Файл не найден: {file_path}")
    with path.open("r", encoding="utf-8") as f:
        return parse_soak_records(f, warmup_sec=warmup_sec)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Отчёт о результатах длительного пилотного прогона (soak test).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Пример минимального JSONL:\n"
            + "\n".join(f"  {json.dumps(row, ensure_ascii=False)}" for row in MINIMAL_JSONL_FORMAT)
            + f"\n\nПример вызова:\n  {EXAMPLE_INVOCATION}\n"
        ),
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Путь к локальному JSONL файлу прогона",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="file_opt",
        default=None,
        help="Альтернативное указание пути к JSONL файлу",
    )
    parser.add_argument(
        "--warmup-sec",
        type=float,
        default=DEFAULT_WARMUP_SEC,
        help=f"Длительность прогрева в секундах (по умолчанию: {DEFAULT_WARMUP_SEC})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывод результатов в формате JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Возвращать ненулевой код возврата при статусе fail или incomplete",
    )

    args = parser.parse_args(argv)
    target_path = args.file or args.file_opt

    if not target_path:
        error_msg = "Укажите путь к локальному файлу JSONL (позиционным аргументом или через --file)"
        if args.json:
            print(json.dumps({"status": "error", "error": error_msg}, ensure_ascii=False))
        else:
            print(f"Ошибка: {error_msg}", file=sys.stderr)
            parser.print_usage(file=sys.stderr)
        return 2

    try:
        report = parse_soak_file(target_path, warmup_sec=args.warmup_sec)
    except FileNotFoundError as e:
        if args.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except SchemaError as e:
        if args.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print(f"Ошибка схемы: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        if args.json:
            print(json.dumps({"status": "error", "error": f"Внутренняя ошибка: {e}"}, ensure_ascii=False))
        else:
            print(f"Непредвиденная ошибка: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(report.to_json())
    else:
        print(report.to_text())

    if args.strict and report.status != "pass":
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
