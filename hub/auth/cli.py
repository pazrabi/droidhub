"""CLI: python -m hub.auth hash."""

from __future__ import annotations

import argparse
import getpass
import io
import logging
import sys


from hub.auth.passwords import hash_password

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """CLI интерфейс модуля hub.auth."""
    parser = argparse.ArgumentParser(
        prog="python -m hub.auth",
        description="CLI авторизации DroidHub",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hash", help="Спросить пароль через getpass и напечатать хэш для .env")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    if args.command == "hash":
        try:
            if sys.stdin.isatty():
                pw = getpass.getpass("Пароль: ")
            elif getattr(getpass.getpass, "__module__", None) != "getpass":
                pw = getpass.getpass("Пароль: ")
            else:
                pw = sys.stdin.readline().rstrip("\r\n")
        except (io.UnsupportedOperation, OSError):
            pw = sys.stdin.readline().rstrip("\r\n")

        if not pw:
            print("Ошибка: пароль не может быть пустым", file=sys.stderr)
            return 1

        hashed = hash_password(pw)
        print(hashed)
        return 0

    return 1

