"""Поиск и проверка внешних утилит adb и scrcpy для macOS, Windows и Linux."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class BinaryInfo:
    """Информация о найденном исполняемом файле."""
    name: str
    found: bool
    path: str | None = None
    version: str | None = None
    error: str | None = None


def get_adb_candidates() -> list[str]:
    """Возвращает список потенциальных путей к adb для текущей ОС."""
    system = platform.system()
    candidates: list[str] = []

    # Пользовательский путь из переменной окружения
    env_path = os.environ.get("DROIDHUB_ADB_PATH")
    if env_path:
        candidates.append(os.path.expanduser(env_path))

    # Стандартный путь из PATH
    which_adb = shutil.which("adb")
    if which_adb:
        candidates.append(which_adb)

    if system == "Darwin":  # macOS
        candidates.extend([
            os.path.expanduser("~/android-platform-tools/platform-tools/adb"),
            os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
            "/opt/homebrew/bin/adb",
            "/usr/local/bin/adb",
        ])
    elif system == "Windows":  # Windows
        which_win = shutil.which("adb.exe")
        if which_win:
            candidates.append(which_win)
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        if local_app:
            candidates.append(os.path.join(local_app, "Android", "Sdk", "platform-tools", "adb.exe"))
        candidates.extend([
            os.path.join(prog_files, "Android", "platform-tools", "adb.exe"),
            r"C:\platform-tools\adb.exe",
            r"C:\Program Files\Android\platform-tools\adb.exe",
        ])
    else:  # Linux и др.
        candidates.extend([
            "/usr/bin/adb",
            "/usr/local/bin/adb",
            os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        ])

    return candidates


def get_scrcpy_candidates() -> list[str]:
    """Возвращает список потенциальных путей к scrcpy для текущей ОС."""
    system = platform.system()
    candidates: list[str] = []

    # Пользовательский путь из переменной окружения
    env_path = os.environ.get("DROIDHUB_SCRCPY_PATH")
    if env_path:
        candidates.append(os.path.expanduser(env_path))

    # Стандартный путь из PATH
    which_scrcpy = shutil.which("scrcpy")
    if which_scrcpy:
        candidates.append(which_scrcpy)

    if system == "Darwin":  # macOS
        candidates.extend([
            "/opt/homebrew/bin/scrcpy",
            "/usr/local/bin/scrcpy",
            os.path.expanduser("~/homebrew/bin/scrcpy"),
        ])
    elif system == "Windows":  # Windows
        which_win = shutil.which("scrcpy.exe")
        if which_win:
            candidates.append(which_win)
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_data = os.environ.get("ProgramData", r"C:\ProgramData")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        if local_app:
            candidates.append(os.path.join(local_app, "Programs", "scrcpy", "scrcpy.exe"))
        if prog_data:
            candidates.append(os.path.join(prog_data, "chocolatey", "bin", "scrcpy.exe"))
        candidates.extend([
            os.path.join(prog_files, "scrcpy", "scrcpy.exe"),
            r"C:\scrcpy\scrcpy.exe",
        ])
    else:  # Linux и др.
        candidates.extend([
            "/usr/bin/scrcpy",
            "/usr/local/bin/scrcpy",
            "/snap/bin/scrcpy",
        ])

    return candidates


def probe_version(cmd: list[str]) -> str | None:
    """Выполняет команду без shell=True и возвращает первую строку вывода версии."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3.0,
            check=False,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        if output:
            return output.splitlines()[0]
    except Exception:
        pass
    return None


def find_adb(custom_path: str | None = None) -> BinaryInfo:
    """Находит adb и проверяет его работоспособность."""
    if custom_path:
        expanded = os.path.expanduser(custom_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            ver = probe_version([expanded, "version"])
            return BinaryInfo(name="adb", found=True, path=expanded, version=ver)
        return BinaryInfo(
            name="adb",
            found=False,
            path=custom_path,
            error=f"Указанный файл adb не найден или не исполняемый: {custom_path}",
        )

    for path in get_adb_candidates():
        if os.path.isfile(path) and os.access(path, os.X_OK):
            ver = probe_version([path, "version"])
            return BinaryInfo(name="adb", found=True, path=path, version=ver)

    return BinaryInfo(
        name="adb",
        found=False,
        error="Утилита ADB не найдена. Установите Android Platform-Tools или укажите путь.",
    )


def find_scrcpy(custom_path: str | None = None) -> BinaryInfo:
    """Находит scrcpy и проверяет его работоспособность."""
    if custom_path:
        expanded = os.path.expanduser(custom_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            ver = probe_version([expanded, "--version"])
            return BinaryInfo(name="scrcpy", found=True, path=expanded, version=ver)
        return BinaryInfo(
            name="scrcpy",
            found=False,
            path=custom_path,
            error=f"Указанный файл scrcpy не найден или не исполняемый: {custom_path}",
        )

    for path in get_scrcpy_candidates():
        if os.path.isfile(path) and os.access(path, os.X_OK):
            ver = probe_version([path, "--version"])
            return BinaryInfo(name="scrcpy", found=True, path=path, version=ver)

    return BinaryInfo(
        name="scrcpy",
        found=False,
        error="Утилита scrcpy не найдена. Установите scrcpy или укажите путь.",
    )


def get_installation_guide() -> str:
    """Возвращает понятное руководство по установке утилит и настройке USB debugging."""
    return """=== РУКОВОДСТВО ПО НАСТРОЙКЕ DROIDHUB DESKTOP ===

1. УСТАНОВКА ADB (Android Debug Bridge):
   • macOS:
     Вариант 1 (Homebrew): выполните команду:
       brew install android-platform-tools
     Вариант 2: скачайте официальный архив Google SDK Platform-Tools:
       https://developer.android.com/tools/releases/platform-tools
       и распакуйте в: ~/android-platform-tools/platform-tools/adb
   • Windows:
     Вариант 1 (winget):
       winget install Google.PlatformTools
     Вариант 2: скачайте SDK Platform-Tools от Google,
       распакуйте в C:\\platform-tools и добавьте эту папку в переменную PATH.
   • Linux:
       sudo apt install adb

2. УСТАНОВКА SCRCPY:
   • macOS:
       brew install scrcpy
   • Windows:
     Вариант 1 (winget):
       winget install Genymobile.scrcpy
     Вариант 2 (Chocolatey):
       choco install scrcpy
     Вариант 3: скачайте zip-релиз с GitHub (https://github.com/Genymobile/scrcpy/releases),
       распакуйте в C:\\scrcpy и добавьте в PATH.
   • Linux:
       sudo apt install scrcpy

3. НАСТРОЙКА ANDROID-ТЕЛЕФОНА (Отладка по USB):
   1. Откройте «Настройки» -> «О телефоне» (или «Сведения о ПО»).
   2. Найдите строку «Номер сборки» и нажмите на неё 7 раз подряд,
      пока не появится надпись «Вы стали разработчиком!».
   3. Вернитесь назад: «Настройки» -> «Система» -> «Для разработчиков».
   4. Включите пункт «Отладка по USB».
   5. Подключите телефон к компьютеру оригинальным кабелем (в режиме «Передача файлов»).
   6. На экране телефона появится диалог:
      «Разрешить отладку по USB с этого компьютера?».
      Поставьте галочку «Всегда разрешать с этого компьютера» и нажмите «Разрешить».

4. ОГРАНИЧЕНИЯ И БЕЗОПАСНОСТЬ:
   • Программа работает только локально, без рута и без adb shell/tcpip.
   • Автоматическое открытие scrcpy ограничено только USB-устройствами.
   • Сетевые ADB-устройства не открываются автоматически.
   • При закрытии программы все запущенные окна scrcpy будут остановлены.
"""
