#!/usr/bin/env python3
"""Autonomous utility for recording physical USB pilot events by an operator.

Python stdlib only, autonomous from desktop/ and ADB.
CLI: --output NEW_PATH
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, TextIO, Tuple

VALID_EDGES = frozenset({"disconnect", "reconnect"})
VALID_KINDS = frozenset({"marker", "adb", "park"})

HELP_DESCRIPTION = (
    "Утилита ручной записи событий физического USB-пилота оператором.\n"
    "Инструмент намеренно ручной: задержка включает реакцию операторов, "
    "отметки дают диагностическую оценку."
)

HELP_EPILOG = (
    "Пример запуска:\n"
    "  python -m scripts.record_desktop_usb_hotplug --output /path/to/events.jsonl\n\n"
    "Пример ввода через stdin:\n"
    "  1 disconnect marker\n"
    "  1 disconnect adb\n"
    "  1 disconnect park\n"
    "  1 reconnect marker\n"
    "  1 reconnect adb\n"
    "  1 reconnect park\n"
    "  quit\n\n"
    "Формат JSONL:\n"
    "  {\"cycle\": 1, \"edge\": \"disconnect\", \"kind\": \"marker\", \"elapsed_sec\": 0.123}\n"
)


def get_parser() -> argparse.ArgumentParser:
    """Create command line argument parser."""
    parser = argparse.ArgumentParser(
        prog="record_desktop_usb_hotplug",
        description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        metavar="NEW_PATH",
        help="Путь к новому выходному файлу JSONL (создаётся исключительно с 0600 O_EXCL).",
    )
    return parser


def create_output_file(path: str) -> TextIO:
    """Atomically create a new output file with 0600 permissions and O_EXCL.

    Refuses to follow symlinks (even broken ones) and fails if the file exists.
    """
    if not path:
        raise ValueError("Output path must not be empty")

    # Explicit check for symlink to refuse following broken or intact symlinks
    if os.path.islink(path):
        raise FileExistsError(f"Target path is a symlink: {path}")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    return open(fd, "w", encoding="utf-8")


def parse_event_tokens(parts: List[str]) -> Tuple[int, str, str]:
    """Parse and validate line tokens: cycle, edge, kind.

    Raises ValueError if invalid.
    """
    if len(parts) != 3:
        raise ValueError(f"Expected exactly 3 tokens (cycle edge kind), got {len(parts)}")

    cycle_str, edge, kind = parts

    try:
        cycle = int(cycle_str)
        if cycle <= 0:
            raise ValueError(f"Cycle must be positive int, got {cycle}")
    except ValueError as exc:
        raise ValueError(f"Invalid cycle {cycle_str!r}: must be positive int") from exc

    if edge not in VALID_EDGES:
        raise ValueError(
            f"Invalid edge {edge!r}: must be one of {sorted(VALID_EDGES)}"
        )

    if kind not in VALID_KINDS:
        raise ValueError(
            f"Invalid kind {kind!r}: must be one of {sorted(VALID_KINDS)}"
        )

    return cycle, edge, kind


def record_events(
    output_path: str,
    input_stream: Optional[Any] = None,
    time_func: Optional[Callable[[], float]] = None,
    err_stream: Optional[TextIO] = None,
) -> List[Dict[str, Any]]:
    """Record operator hotplug events to a JSONL file.

    Parameters:
        output_path: Path to new file (must not exist).
        input_stream: Source of lines (default sys.stdin).
        time_func: Clock function (default time.monotonic).
        err_stream: Stream for diagnostic warnings/errors (default sys.stderr).
    """
    if input_stream is None:
        input_stream = sys.stdin
    if time_func is None:
        time_func = time.monotonic
    if err_stream is None:
        err_stream = sys.stderr

    out_file = create_output_file(output_path)
    recorded: List[Dict[str, Any]] = []
    seen_keys: Set[Tuple[int, str, str]] = set()

    has_readline = hasattr(input_stream, "readline")
    input_iter = None if has_readline else iter(input_stream)

    start_time = time_func()

    try:
        while True:
            try:
                if has_readline:
                    line = input_stream.readline()
                else:
                    line = next(input_iter)
            except (StopIteration, KeyboardInterrupt):
                break

            # Immediate timestamp relative to start upon reading line
            read_time = time_func()

            if not line:
                break

            text = line.strip()
            if not text:
                continue

            if text == "quit":
                break

            elapsed = read_time - start_time
            if not math.isfinite(elapsed) or elapsed < 0:
                elapsed = 0.0
            elapsed_sec = round(float(elapsed), 9)

            parts = text.split()
            try:
                cycle, edge, kind = parse_event_tokens(parts)
            except ValueError as err:
                err_stream.write(f"Rejected line {text!r}: {err}\n")
                err_stream.flush()
                continue

            key = (cycle, edge, kind)
            if key in seen_keys:
                err_stream.write(
                    f"Rejected duplicate event: cycle={cycle} edge={edge} kind={kind}\n"
                )
                err_stream.flush()
                continue

            seen_keys.add(key)
            event_obj = {
                "cycle": cycle,
                "edge": edge,
                "kind": kind,
                "elapsed_sec": elapsed_sec,
            }
            out_file.write(json.dumps(event_obj) + "\n")
            out_file.flush()
            recorded.append(event_obj)
    except KeyboardInterrupt:
        pass
    finally:
        out_file.flush()
        out_file.close()

    return recorded


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    parser = get_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        record_events(args.output)
        return 0
    except OSError as err:
        sys.stderr.write(f"IO error: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
