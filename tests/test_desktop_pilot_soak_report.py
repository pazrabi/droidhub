"""
tests/test_desktop_pilot_soak_report.py - Тесты скрипта desktop_pilot_soak_report.py.

Проверяет:
- Валидный длинный набор (pass, >= 8 ч, >= 100 запусков, >= 20 циклов, <= 20% RSS, 0 crashes/lost_runs/remaining_children)
- Валидный короткий набор (не считать smoke приёмкой -> incomplete)
- Отсутствие shutdown (incomplete)
- Нарушения схемы (отсутствие/невалидность elapsed_sec, немонотонность, недопустимый type/status,
  отсутствие/неположительный rss_mb, дубли start/shutdown, записи после shutdown, невалидный JSON)
- Парность disconnect/reconnect (полные циклы, непарные disconnect, непарные reconnect)
- Граничные условия (8 ч ровно / 28799 с, 100 / 99 запусков, 20 / 19 циклов, 20.0% / 20.01% RSS,
  пороги crashes, lost_runs, remaining_children, граница прогрева)
- Отсутствие сырых данных и ID в выводе
- Наличие минимального формата JSONL и примера вызова в отчёте
- Работу CLI (--json, --warmup-sec, --file, --strict, обработку ошибок)
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from scripts.desktop_pilot_soak_report import (
    DEFAULT_WARMUP_SEC,
    EXAMPLE_INVOCATION,
    MINIMAL_JSONL_FORMAT,
    RSS_FORMULA_STR,
    THRESHOLD_CRASHES,
    THRESHOLD_DURATION_SEC,
    THRESHOLD_FULL_CYCLES,
    THRESHOLD_LOST_RUNS,
    THRESHOLD_REMAINING_CHILDREN,
    THRESHOLD_RSS_GROWTH_PCT,
    THRESHOLD_TOTAL_RUNS,
    SchemaError,
    main,
    parse_soak_file,
    parse_soak_records,
)


def _make_jsonl(records: list[dict[str, Any]]) -> str:
    """Сериализует список словарей в JSONL-строку."""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def _write_jsonl_file(path: Path, records: list[dict[str, Any]]) -> Path:
    """Записывает список записей в локальный JSONL-файл."""
    path.write_text(_make_jsonl(records), encoding="utf-8")
    return path


def _generate_valid_long_records(
    duration_sec: float = 28800.0,
    num_runs: int = 100,
    num_cycles: int = 20,
    initial_rss: float = 100.0,
    max_rss: float = 115.0,  # +15% рост (порог 20%)
    crashes: int = 0,
    lost_runs: int = 0,
    remaining_children: int = 0,
    warmup_sec: float = 600.0,
) -> list[dict[str, Any]]:
    """Генерирует хронологически корректный набор для длительного прогона."""
    records: list[dict[str, Any]] = [{"elapsed_sec": 0.0, "type": "start"}]

    # До прогрева: образец RSS
    records.append({"elapsed_sec": 300.0, "type": "sample", "rss_mb": 90.0})

    # На прогреве: первый post-warmup образец
    records.append({"elapsed_sec": warmup_sec, "type": "sample", "rss_mb": initial_rss})

    # Равномерно распределяем запуски и циклы от warmup_sec до duration_sec - 100
    time_span = max(1.0, (duration_sec - 100.0) - warmup_sec)
    step = time_span / max(num_runs, num_cycles, 1)

    # Добавляем запуски
    for i in range(num_runs):
        t = warmup_sec + (i + 0.2) * step
        status = "succeeded" if i % 10 != 0 else "partial"
        records.append({"elapsed_sec": round(t, 2), "type": "run", "status": status})

    # Добавляем циклы disconnect/reconnect
    for i in range(num_cycles):
        t_disc = warmup_sec + (i + 0.4) * step
        t_rec = warmup_sec + (i + 0.6) * step
        records.append({"elapsed_sec": round(t_disc, 2), "type": "disconnect"})
        records.append({"elapsed_sec": round(t_rec, 2), "type": "reconnect"})

    # Добавляем промежуточные и пиковый образец RSS
    mid_time = warmup_sec + time_span * 0.5
    records.append({"elapsed_sec": round(mid_time, 2), "type": "sample", "rss_mb": max_rss})
    records.append({"elapsed_sec": round(duration_sec - 50.0, 2), "type": "sample", "rss_mb": initial_rss + 5.0})

    # Сортируем хронологически
    records.sort(key=lambda r: r["elapsed_sec"])

    # Финальная запись shutdown
    records.append({
        "elapsed_sec": duration_sec,
        "type": "shutdown",
        "crashes": crashes,
        "lost_runs": lost_runs,
        "remaining_children": remaining_children,
    })

    return records


# =====================================================================
# 1. Валидный длинный набор (pass)
# =====================================================================


def test_valid_long_set_passes() -> None:
    records = _generate_valid_long_records(
        duration_sec=28800.0,
        num_runs=105,
        num_cycles=22,
        initial_rss=100.0,
        max_rss=118.0,  # 18% рост
        crashes=0,
        lost_runs=0,
        remaining_children=0,
    )
    lines = _make_jsonl(records).splitlines()
    report = parse_soak_records(lines)

    assert report.status == "pass"
    assert report.input_completeness.is_complete is True
    assert report.input_completeness.has_start is True
    assert report.input_completeness.has_shutdown is True
    assert report.input_completeness.is_shutdown_final is True

    assert report.duration.duration_sec == 28800.0
    assert report.duration.duration_hours == 8.0
    assert report.duration.threshold_passed is True

    assert report.runs.total_runs == 105
    assert report.runs.threshold_passed is True
    assert report.runs.succeeded_runs > 0
    assert report.runs.success_rate_pct > 80.0

    assert report.cycles.full_cycles == 22
    assert report.cycles.threshold_passed is True
    assert report.cycles.unpaired_disconnects == 0
    assert report.cycles.unpaired_reconnects == 0

    assert report.rss.post_warmup_samples_count >= 2
    assert report.rss.first_rss_mb == 100.0
    assert report.rss.max_rss_mb == 118.0
    assert report.rss.growth_pct == pytest.approx(18.0)
    assert report.rss.threshold_passed is True

    assert report.shutdown.crashes == 0
    assert report.shutdown.lost_runs == 0
    assert report.shutdown.remaining_children == 0
    assert report.shutdown.zero_crashes_passed is True
    assert report.shutdown.zero_lost_runs_passed is True
    assert report.shutdown.zero_remaining_children_passed is True

    assert report.thresholds_summary.all_thresholds_passed is True
    assert len(report.reasons) == 0


# =====================================================================
# 2. Валидный короткий набор (smoke != пилотная приёмка -> incomplete)
# =====================================================================


def test_valid_short_set_is_incomplete_not_accepted() -> None:
    """Единичный короткий smoke не должен считаться пилотной приёмкой (status: incomplete)."""
    short_records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 30.0, "type": "sample", "rss_mb": 100.0},
        {"elapsed_sec": 50.0, "type": "run", "status": "succeeded"},
        {"elapsed_sec": 70.0, "type": "disconnect"},
        {"elapsed_sec": 90.0, "type": "reconnect"},
        {"elapsed_sec": 120.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    lines = _make_jsonl(short_records).splitlines()
    report = parse_soak_records(lines)

    assert report.status == "incomplete"
    assert report.input_completeness.is_complete is True
    assert report.duration.threshold_passed is False
    assert report.runs.threshold_passed is False
    assert report.cycles.threshold_passed is False
    assert report.rss.threshold_passed is None  # нет измерений после прогрева 600 с
    assert len(report.reasons) > 0


def test_short_smoke_with_crash_is_fail() -> None:
    """Если даже в коротком smoke был crash, то это явный fail (порог нарушен)."""
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 30.0, "type": "run", "status": "succeeded"},
        {"elapsed_sec": 60.0, "type": "shutdown", "crashes": 1, "lost_runs": 0, "remaining_children": 0},
    ]
    lines = _make_jsonl(records).splitlines()
    report = parse_soak_records(lines)
    assert report.status == "fail"
    assert any("crashes" in r for r in report.reasons)


# =====================================================================
# 3. Отсутствие shutdown (incomplete)
# =====================================================================


def test_missing_shutdown_is_incomplete() -> None:
    """Обрыв файла или незавершённый прогон без shutdown -> incomplete."""
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 600.0, "type": "sample", "rss_mb": 150.0},
        {"elapsed_sec": 650.0, "type": "run", "status": "succeeded"},
        {"elapsed_sec": 700.0, "type": "disconnect"},
        {"elapsed_sec": 720.0, "type": "reconnect"},
    ]
    lines = _make_jsonl(records).splitlines()
    report = parse_soak_records(lines)

    assert report.status == "incomplete"
    assert report.input_completeness.has_shutdown is False
    assert report.input_completeness.is_shutdown_final is False
    assert report.input_completeness.is_complete is False
    assert any("shutdown" in r for r in report.reasons)


def test_missing_start_is_incomplete() -> None:
    records = [
        {"elapsed_sec": 600.0, "type": "sample", "rss_mb": 150.0},
        {"elapsed_sec": 28800.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    report = parse_soak_records(_make_jsonl(records).splitlines())
    assert report.status == "incomplete"
    assert report.input_completeness.has_start is False


# =====================================================================
# 4. Нарушения схемы (SchemaError)
# =====================================================================


@pytest.mark.parametrize(
    "bad_line, err_match",
    [
        ('{"type": "start"}', "Отсутствует обязательное поле 'elapsed_sec'"),
        ('{"elapsed_sec": -10, "type": "start"}', "неотрицательным"),
        ('{"elapsed_sec": "10", "type": "start"}', "должно быть числом"),
        ('{"elapsed_sec": true, "type": "start"}', "должно быть числом"),
        ('{"elapsed_sec": 1e999, "type": "start"}', "конечным числом"),
        ('{"elapsed_sec": 0}', "Отсутствует обязательное поле 'type'"),
        ('{"elapsed_sec": 0, "type": "unknown_type"}', "Недопустимый type"),
        ('{"elapsed_sec": 10, "type": "sample"}', "отсутствует обязательное поле 'rss_mb'"),
        ('{"elapsed_sec": 10, "type": "sample", "rss_mb": 0.0}', "положительным числом"),
        ('{"elapsed_sec": 10, "type": "sample", "rss_mb": -5.0}', "положительным числом"),
        ('{"elapsed_sec": 10, "type": "sample", "rss_mb": "150"}', "должно быть числом"),
        ('{"elapsed_sec": 10, "type": "run"}', "отсутствует обязательное поле 'status'"),
        ('{"elapsed_sec": 10, "type": "run", "status": "unknown"}', "Недопустимый status"),
        ('{"elapsed_sec": 10, "type": "shutdown", "crashes": 0, "lost_runs": 0}', "отсутствует обязательное поле 'remaining_children'"),
        ('{"elapsed_sec": 10, "type": "shutdown", "crashes": -1, "lost_runs": 0, "remaining_children": 0}', "неотрицательным"),
        ('{"elapsed_sec": 10, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 1.5}', "целым числом"),
        ('{"elapsed_sec": 10, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": true}', "целым числом"),
        ("not a json", "Невалидный JSON"),
        ("[1, 2, 3]", "должна быть JSON-объектом"),
    ],
)
def test_schema_violations(bad_line: str, err_match: str) -> None:
    with pytest.raises(SchemaError, match=err_match):
        parse_soak_records([bad_line])


def test_non_monotonic_elapsed_sec_raises() -> None:
    """Уменьшение elapsed_sec нарушает хронологическую монотонность."""
    records = [
        {"elapsed_sec": 100.0, "type": "start"},
        {"elapsed_sec": 90.0, "type": "sample", "rss_mb": 150.0},
    ]
    with pytest.raises(SchemaError, match="Нарушение монотонности"):
        parse_soak_records(_make_jsonl(records).splitlines())


def test_duplicate_start_raises() -> None:
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 10.0, "type": "start"},
    ]
    with pytest.raises(SchemaError, match="Повторная запись 'start'"):
        parse_soak_records(_make_jsonl(records).splitlines())


def test_records_after_shutdown_raises() -> None:
    """shutdown должен быть финальной записью."""
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 100.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
        {"elapsed_sec": 110.0, "type": "sample", "rss_mb": 120.0},
    ]
    with pytest.raises(SchemaError, match="после shutdown недопустима"):
        parse_soak_records(_make_jsonl(records).splitlines())


def test_duplicate_shutdown_raises() -> None:
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 100.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
        {"elapsed_sec": 110.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    with pytest.raises(SchemaError, match="после shutdown недопустима"):
        parse_soak_records(_make_jsonl(records).splitlines())


# =====================================================================
# 5. Парность disconnect/reconnect
# =====================================================================


def test_pairing_clean_pairs() -> None:
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 10.0, "type": "disconnect"},
        {"elapsed_sec": 20.0, "type": "reconnect"},
        {"elapsed_sec": 30.0, "type": "disconnect"},
        {"elapsed_sec": 40.0, "type": "reconnect"},
        {"elapsed_sec": 50.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    report = parse_soak_records(_make_jsonl(records).splitlines())
    assert report.cycles.full_cycles == 2
    assert report.cycles.disconnect_count == 2
    assert report.cycles.reconnect_count == 2
    assert report.cycles.unpaired_disconnects == 0
    assert report.cycles.unpaired_reconnects == 0


def test_pairing_unpaired_disconnect_and_reconnect() -> None:
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        # Непарный reconnect в начале
        {"elapsed_sec": 5.0, "type": "reconnect"},
        # Корректная пара
        {"elapsed_sec": 10.0, "type": "disconnect"},
        {"elapsed_sec": 20.0, "type": "reconnect"},
        # Непарный disconnect в конце (без последующего reconnect)
        {"elapsed_sec": 30.0, "type": "disconnect"},
        {"elapsed_sec": 40.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    report = parse_soak_records(_make_jsonl(records).splitlines())
    assert report.cycles.full_cycles == 1
    assert report.cycles.disconnect_count == 2
    assert report.cycles.reconnect_count == 2
    assert report.cycles.unpaired_disconnects == 1
    assert report.cycles.unpaired_reconnects == 1


# =====================================================================
# 6. Граничные условия (Boundaries)
# =====================================================================


def test_boundary_duration_8_hours_exact() -> None:
    """Ровно 8 часов (28800.0 с) удовлетворяет порогу."""
    records = _generate_valid_long_records(duration_sec=28800.0)
    report = parse_soak_records(_make_jsonl(records).splitlines())
    assert report.duration.duration_sec == 28800.0
    assert report.duration.threshold_passed is True
    assert report.status == "pass"


def test_boundary_duration_just_below_8_hours() -> None:
    """28799.0 с не удовлетворяет порогу длительности -> incomplete."""
    records = _generate_valid_long_records(duration_sec=28799.0)
    report = parse_soak_records(_make_jsonl(records).splitlines())
    assert report.duration.duration_sec == 28799.0
    assert report.duration.threshold_passed is False
    assert report.status == "incomplete"
    assert any("Длительность недостаточна" in r for r in report.reasons)


def test_boundary_runs_exact_100_and_99() -> None:
    # 100 запусков -> pass
    records_100 = _generate_valid_long_records(num_runs=100)
    report_100 = parse_soak_records(_make_jsonl(records_100).splitlines())
    assert report_100.runs.total_runs == 100
    assert report_100.runs.threshold_passed is True
    assert report_100.status == "pass"

    # 99 запусков -> incomplete
    records_99 = _generate_valid_long_records(num_runs=99)
    report_99 = parse_soak_records(_make_jsonl(records_99).splitlines())
    assert report_99.runs.total_runs == 99
    assert report_99.runs.threshold_passed is False
    assert report_99.status == "incomplete"


def test_boundary_cycles_exact_20_and_19() -> None:
    # 20 циклов -> pass
    records_20 = _generate_valid_long_records(num_cycles=20)
    report_20 = parse_soak_records(_make_jsonl(records_20).splitlines())
    assert report_20.cycles.full_cycles == 20
    assert report_20.cycles.threshold_passed is True
    assert report_20.status == "pass"

    # 19 циклов -> incomplete
    records_19 = _generate_valid_long_records(num_cycles=19)
    report_19 = parse_soak_records(_make_jsonl(records_19).splitlines())
    assert report_19.cycles.full_cycles == 19
    assert report_19.cycles.threshold_passed is False
    assert report_19.status == "incomplete"


def test_boundary_rss_growth_exact_20_and_20_01() -> None:
    # Ровно 20.0% рост: 100 -> 120.0 -> pass
    records_20_pct = _generate_valid_long_records(initial_rss=100.0, max_rss=120.0)
    report_20_pct = parse_soak_records(_make_jsonl(records_20_pct).splitlines())
    assert report_20_pct.rss.growth_pct == pytest.approx(20.0)
    assert report_20_pct.rss.threshold_passed is True
    assert report_20_pct.status == "pass"

    # 20.1% рост: 100 -> 120.1 -> fail
    records_20_1_pct = _generate_valid_long_records(initial_rss=100.0, max_rss=120.1)
    report_20_1_pct = parse_soak_records(_make_jsonl(records_20_1_pct).splitlines())
    assert report_20_1_pct.rss.growth_pct == pytest.approx(20.1)
    assert report_20_1_pct.rss.threshold_passed is False
    assert report_20_1_pct.status == "fail"


@pytest.mark.parametrize("crashes,lost_runs,remaining_children", [
    (1, 0, 0),
    (0, 1, 0),
    (0, 0, 1),
])
def test_boundary_shutdown_faults_cause_fail(
    crashes: int, lost_runs: int, remaining_children: int
) -> None:
    records = _generate_valid_long_records(
        crashes=crashes,
        lost_runs=lost_runs,
        remaining_children=remaining_children,
    )
    report = parse_soak_records(_make_jsonl(records).splitlines())
    assert report.status == "fail"


def test_warmup_boundary_inclusion() -> None:
    """Образец ровно в warmup_sec включается в расчёт post-warmup RSS."""
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 599.9, "type": "sample", "rss_mb": 500.0},  # до прогрева
        {"elapsed_sec": 600.0, "type": "sample", "rss_mb": 100.0},  # ровно на прогреве -> first_rss
        {"elapsed_sec": 700.0, "type": "sample", "rss_mb": 110.0},
        {"elapsed_sec": 1000.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    report = parse_soak_records(_make_jsonl(records).splitlines(), warmup_sec=600.0)
    assert report.rss.post_warmup_samples_count == 2
    assert report.rss.first_rss_mb == 100.0
    assert report.rss.max_rss_mb == 110.0
    assert report.rss.growth_pct == pytest.approx(10.0)


def test_configurable_warmup_sec() -> None:
    """Параметр warmup_sec настраивается (например, 300 с)."""
    records = [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 299.0, "type": "sample", "rss_mb": 500.0},
        {"elapsed_sec": 300.0, "type": "sample", "rss_mb": 200.0},
        {"elapsed_sec": 350.0, "type": "sample", "rss_mb": 210.0},
        {"elapsed_sec": 400.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    report = parse_soak_records(_make_jsonl(records).splitlines(), warmup_sec=300.0)
    assert report.rss.warmup_sec == 300.0
    assert report.rss.first_rss_mb == 200.0
    assert report.rss.max_rss_mb == 210.0
    assert report.rss.growth_pct == pytest.approx(5.0)


# =====================================================================
# 7. Отсутствие сырых данных и ID в выводе
# =====================================================================


def test_output_contains_no_raw_data_or_ids() -> None:
    """Вывод не должен содержать сырых строк или идентификаторов (device_id, run_id)."""
    records = [
        {"elapsed_sec": 0.0, "type": "start", "device_id": "phone-pixel-007", "raw_dump": "secret_abc"},
        {"elapsed_sec": 600.0, "type": "sample", "rss_mb": 100.0, "sensor_id": "mem-1"},
        {"elapsed_sec": 700.0, "type": "run", "status": "succeeded", "run_id": "run-999"},
        {"elapsed_sec": 28800.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ]
    report = parse_soak_records(_make_jsonl(records).splitlines())

    json_str = report.to_json()
    text_str = report.to_text()

    for forbidden in ("phone-pixel-007", "secret_abc", "mem-1", "run-999"):
        assert forbidden not in json_str, f"Запрещённый ID '{forbidden}' найден в JSON"
        assert forbidden not in text_str, f"Запрещённый ID '{forbidden}' найден в текстовом отчёте"

    # Проверяем, что в JSON нет ключей 'device_id', 'run_id', 'id'
    report_dict = report.to_dict()
    assert "device_id" not in report_dict
    assert "run_id" not in report_dict
    assert "id" not in report_dict


# =====================================================================
# 8. Наличие минимального формата JSONL, примера вызова и формулы RSS
# =====================================================================


def test_report_includes_minimal_jsonl_and_example_call() -> None:
    records = _generate_valid_long_records()
    report = parse_soak_records(_make_jsonl(records).splitlines())

    # В объекте отчёта
    assert report.minimal_jsonl_format == MINIMAL_JSONL_FORMAT
    assert report.example_invocation == EXAMPLE_INVOCATION
    assert report.rss.formula == RSS_FORMULA_STR

    # В текстовом представлении
    text = report.to_text()
    assert "МИНИМАЛЬНЫЙ ФОРМАТ JSONL:" in text
    assert "ПРИМЕР ВЫЗОВА:" in text
    assert "scripts/desktop_pilot_soak_report.py" in text
    assert "((max_rss_mb - first_rss_mb) / first_rss_mb) * 100" in text

    # В JSON представлении
    data = json.loads(report.to_json())
    assert "minimal_jsonl_format" in data
    assert "example_invocation" in data
    assert data["rss"]["formula"] == RSS_FORMULA_STR


# =====================================================================
# 9. Тесты CLI интерфейса
# =====================================================================


def test_cli_human_readable_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = _write_jsonl_file(tmp_path / "valid.jsonl", _generate_valid_long_records())
    ret = main([str(log_file)])
    assert ret == 0

    captured = capsys.readouterr()
    assert "ОТЧЁТ О ДЛИТЕЛЬНОМ ПИЛОТНОМ ПРОГОНЕ" in captured.out
    assert "ИТОГ: PASS" in captured.out


def test_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = _write_jsonl_file(tmp_path / "valid.jsonl", _generate_valid_long_records())
    ret = main([str(log_file), "--json"])
    assert ret == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "pass"
    assert data["duration"]["threshold_passed"] is True
    assert data["runs"]["threshold_passed"] is True
    assert data["cycles"]["threshold_passed"] is True


def test_cli_file_option_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = _write_jsonl_file(tmp_path / "valid.jsonl", _generate_valid_long_records())
    ret = main(["--file", str(log_file), "--json"])
    assert ret == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "pass"


def test_cli_missing_file_arg(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main([])
    assert ret == 2
    captured = capsys.readouterr()
    assert "Укажите путь к локальному файлу JSONL" in captured.err


def test_cli_missing_file_arg_json(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["--json"])
    assert ret == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "error"


def test_cli_file_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["/non/existent/path.jsonl", "--json"])
    assert ret == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "error"
    assert "Файл не найден" in data["error"]


def test_cli_schema_error_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad_file = tmp_path / "bad.jsonl"
    bad_file.write_text('{"type": "start"}\n', encoding="utf-8")
    ret = main([str(bad_file), "--json"])
    assert ret == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "error"
    assert "elapsed_sec" in data["error"]


def test_cli_strict_flag(tmp_path: Path) -> None:
    """--strict возвращает код 1 при статусе incomplete или fail."""
    short_file = _write_jsonl_file(tmp_path / "short.jsonl", [
        {"elapsed_sec": 0.0, "type": "start"},
        {"elapsed_sec": 60.0, "type": "shutdown", "crashes": 0, "lost_runs": 0, "remaining_children": 0},
    ])
    # Без --strict: возвращает 0 (отчёт сформирован)
    assert main([str(short_file)]) == 0
    # С --strict: возвращает 1 (так как status: incomplete)
    assert main([str(short_file), "--strict"]) == 1


def test_cli_subprocess_invocation(tmp_path: Path) -> None:
    """Проверка запуска как отдельного Python-процесса."""
    log_file = _write_jsonl_file(tmp_path / "valid.jsonl", _generate_valid_long_records())
    res = subprocess.run(
        [sys.executable, "scripts/desktop_pilot_soak_report.py", str(log_file), "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(res.stdout)
    assert data["status"] == "pass"
