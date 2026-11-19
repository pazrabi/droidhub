#!/usr/bin/env python3
"""Standalone script to verify macOS release artifact (DroidHub.app).

Checks Info.plist, main executable, bundled tools, arm64 architecture, and code signature.
Uses only Python standard library.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

EXPECTED_BUNDLE_ID = "site.molokohub.droidhub"
EXPECTED_EXECUTABLE = "DroidHub"
REQUIRED_TOOLS = ("adb", "scrcpy", "scrcpy-server", "LICENSE")


class VerificationError(Exception):
    """Raised when release artifact verification fails."""


def check_bundle_symlinks(bundle_root: Path) -> None:
    """Проверяет все символические ссылки внутри bundle.

    Разрешает внутренние symlink (используемые PyInstaller для data),
    но прерывает выполнение при обнаружении битых ссылок, циклов или escape из bundle.
    """
    canonical_root = bundle_root.resolve(strict=True)
    for root, dirs, files in os.walk(bundle_root, followlinks=False):
        for name in dirs + files:
            entry = Path(root) / name
            if entry.is_symlink():
                try:
                    target = entry.resolve(strict=True)
                except FileNotFoundError as e:
                    raise VerificationError(
                        f"Битая символическая ссылка: {entry} -> {os.readlink(entry)}"
                    ) from e
                except RuntimeError as e:
                    raise VerificationError(
                        f"Циклическая символическая ссылка: {entry}"
                    ) from e

                try:
                    target.relative_to(canonical_root)
                except ValueError as e:
                    raise VerificationError(
                        f"Символическая ссылка выходит за пределы bundle (escape): "
                        f"{entry} -> {target}"
                    ) from e


def verify_info_plist(bundle_path: Path) -> dict[str, Any]:
    """Проверяет наличие и корректность Info.plist."""
    info_plist_path = bundle_path / "Contents" / "Info.plist"
    if not info_plist_path.exists():
        raise VerificationError(f"Отсутствует файл Info.plist: {info_plist_path}")
    if not info_plist_path.is_file():
        raise VerificationError(f"Info.plist не является файлом: {info_plist_path}")

    try:
        with open(info_plist_path, "rb") as fp:
            plist_data = plistlib.load(fp)
    except Exception as e:
        raise VerificationError(f"Ошибка чтения или повреждён Info.plist: {e}") from e

    if not isinstance(plist_data, dict):
        raise VerificationError("Некорректная структура Info.plist: ожидался словарь")

    bundle_id = plist_data.get("CFBundleIdentifier")
    if bundle_id != EXPECTED_BUNDLE_ID:
        raise VerificationError(
            f"Неверный или отсутствующий CFBundleIdentifier: "
            f"ожидается '{EXPECTED_BUNDLE_ID}', получено '{bundle_id}'"
        )

    bundle_exec = plist_data.get("CFBundleExecutable")
    if bundle_exec != EXPECTED_EXECUTABLE:
        raise VerificationError(
            f"Неверный или отсутствующий CFBundleExecutable: "
            f"ожидается '{EXPECTED_EXECUTABLE}', получено '{bundle_exec}'"
        )

    return plist_data


def verify_executable(bundle_path: Path, executable_name: str = EXPECTED_EXECUTABLE) -> Path:
    """Проверяет наличие и исполняемость основного бинарного файла Contents/MacOS/DroidHub."""
    macos_dir = bundle_path / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        raise VerificationError(f"Директория Contents/MacOS не найдена: {macos_dir}")

    exec_path = macos_dir / executable_name
    if not exec_path.exists():
        raise VerificationError(f"Исполняемый файл не найден: {exec_path}")
    if not exec_path.is_file():
        raise VerificationError(f"Исполняемый файл не является регулярным файлом: {exec_path}")
    if not os.access(exec_path, os.X_OK):
        raise VerificationError(f"Файл не имеет прав на исполнение (+x): {exec_path}")

    return exec_path


def verify_tools(bundle_path: Path) -> None:
    """Проверяет наличие комплектных инструментов Contents/Frameworks/tools/{adb,scrcpy,scrcpy-server,LICENSE}."""
    tools_dir = bundle_path / "Contents" / "Frameworks" / "tools"
    if not tools_dir.is_dir():
        raise VerificationError(f"Директория комплектных инструментов не найдена: {tools_dir}")

    for tool_name in REQUIRED_TOOLS:
        tool_path = tools_dir / tool_name
        if not tool_path.exists():
            raise VerificationError(f"Отсутствует комплектный инструмент: {tool_path}")
        if not tool_path.is_file():
            raise VerificationError(f"Комплектный инструмент не является файлом: {tool_path}")


def verify_architecture(
    executable_path: Path,
    lipo_bin: str = "/usr/bin/lipo",
    timeout: float = 30.0,
) -> list[str]:
    """Проверяет архитектуру через lipo -archs. Подходят thin arm64 и universal с arm64."""
    cmd = [lipo_bin, "-archs", str(executable_path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise VerificationError(
            f"Не удалось запустить утилиту lipo ('{lipo_bin}'): {e}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise VerificationError(
            f"Превышен таймаут ({timeout} с) при выполнении lipo: {' '.join(cmd)}"
        ) from e

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise VerificationError(
            f"Ошибка lipo при проверке архитектуры {executable_path} (код {proc.returncode}): {err}"
        )

    archs = proc.stdout.strip().split()
    if not archs:
        raise VerificationError(
            f"Утилита lipo не вернула архитектур для файла {executable_path}"
        )

    has_arm64 = any(arch == "arm64" or arch.startswith("arm64") for arch in archs)
    if not has_arm64:
        raise VerificationError(
            f"Неверная архитектура: обнаружено {archs}, требуется arm64 "
            f"(поддерживаются thin arm64 и universal с arm64)"
        )

    return archs


def verify_signature(
    bundle_path: Path,
    codesign_bin: str = "/usr/bin/codesign",
    timeout: float = 30.0,
) -> None:
    """Проверяет валидность подписи через codesign --verify --deep --strict."""
    cmd = [codesign_bin, "--verify", "--deep", "--strict", str(bundle_path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise VerificationError(
            f"Не удалось запустить утилиту codesign ('{codesign_bin}'): {e}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise VerificationError(
            f"Превышен таймаут ({timeout} с) при выполнении codesign: {' '.join(cmd)}"
        ) from e

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise VerificationError(
            f"Ошибка проверки подписи codesign (код {proc.returncode}): {err}"
        )


def verify_bundle(
    bundle_path: str | Path,
    *,
    lipo_bin: str | None = None,
    codesign_bin: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Выполняет полную недеструктивную верификацию macOS bundle."""
    raw_path = Path(bundle_path)
    if not raw_path.exists():
        raise VerificationError(f"Указанный путь к bundle не существует: {bundle_path}")
    if not raw_path.is_dir():
        raise VerificationError(f"Указанный путь к bundle не является директорией: {bundle_path}")

    bundle = raw_path.resolve(strict=True)

    # 1. Проверка структуры symlinks (внутренние разрешены, битые/escape запрещены)
    check_bundle_symlinks(bundle)

    # 2. Недеструктивная проверка Info.plist
    plist_data = verify_info_plist(bundle)

    # 3. Наличие исполняемого бинарного файла Contents/MacOS/DroidHub
    exec_name = plist_data.get("CFBundleExecutable", EXPECTED_EXECUTABLE)
    exec_path = verify_executable(bundle, exec_name)

    # 4. Комплектные инструменты Contents/Frameworks/tools/
    verify_tools(bundle)

    # 5. Проверка архитектуры arm64 через lipo -archs
    lipo_tool = lipo_bin or os.environ.get("DROIDHUB_LIPO_BIN", "/usr/bin/lipo")
    archs = verify_architecture(exec_path, lipo_bin=lipo_tool, timeout=timeout)

    # 6. Валидность цифровой подписи через codesign --verify --deep --strict
    codesign_tool = codesign_bin or os.environ.get("DROIDHUB_CODESIGN_BIN", "/usr/bin/codesign")
    verify_signature(bundle, codesign_bin=codesign_tool, timeout=timeout)

    return {
        "bundle": str(bundle),
        "bundle_id": plist_data.get("CFBundleIdentifier"),
        "executable": str(exec_path),
        "archs": archs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Недеструктивная верификация release-артефакта macOS (arm64 DroidHub.app)."
    )
    parser.add_argument(
        "bundle",
        type=Path,
        help="Путь к bundle DroidHub.app",
    )
    parser.add_argument(
        "--lipo-bin",
        default=None,
        help="Путь к утилите lipo (по умолчанию /usr/bin/lipo или DROIDHUB_LIPO_BIN)",
    )
    parser.add_argument(
        "--codesign-bin",
        default=None,
        help="Путь к утилите codesign (по умолчанию /usr/bin/codesign или DROIDHUB_CODESIGN_BIN)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Таймаут выполнения утилит в секундах (по умолчанию 30.0)",
    )

    args = parser.parse_args(argv)

    try:
        result = verify_bundle(
            bundle_path=args.bundle,
            lipo_bin=args.lipo_bin,
            codesign_bin=args.codesign_bin,
            timeout=args.timeout,
        )
        print(
            f"Верификация macOS release-артефакта успешно пройдена: {result['bundle']} "
            f"(id={result['bundle_id']}, archs={result['archs']})"
        )
        return 0
    except VerificationError as e:
        print(f"Ошибка верификации: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Непредвиденная ошибка при верификации: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
