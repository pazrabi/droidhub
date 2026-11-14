"""Тесты обнаружения утилит adb и scrcpy на macOS, Windows и Linux."""

from __future__ import annotations

import os
import platform
from unittest.mock import MagicMock, patch

from desktop.binaries import (
    BinaryInfo,
    find_adb,
    find_scrcpy,
    get_adb_candidates,
    get_installation_guide,
    get_scrcpy_candidates,
    probe_version,
)


def test_get_adb_candidates_mac_includes_home_path():
    """Проверяет, что на macOS путь ~/android-platform-tools/platform-tools/adb включен."""
    with patch("platform.system", return_value="Darwin"):
        with patch("shutil.which", return_value=None):
            candidates = get_adb_candidates()
            expected_mac_path = os.path.expanduser("~/android-platform-tools/platform-tools/adb")
            assert expected_mac_path in candidates
            assert "/opt/homebrew/bin/adb" in candidates
            assert "/usr/local/bin/adb" in candidates


def test_get_adb_candidates_windows():
    """Проверяет кандидатов adb на Windows."""
    with patch("platform.system", return_value="Windows"):
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\User\AppData\Local"}):
                candidates = get_adb_candidates()
                assert r"C:\platform-tools\adb.exe" in candidates
                assert any("AppData" in c for c in candidates)


def test_get_adb_candidates_env_override():
    """Проверяет приоритет переменной окружения DROIDHUB_ADB_PATH."""
    custom_adb = "/custom/path/to/adb"
    with patch.dict(os.environ, {"DROIDHUB_ADB_PATH": custom_adb}):
        candidates = get_adb_candidates()
        assert candidates[0] == custom_adb


def test_get_scrcpy_candidates_mac_and_windows():
    """Проверяет пути scrcpy для macOS и Windows."""
    with patch("platform.system", return_value="Darwin"):
        with patch("shutil.which", return_value=None):
            mac_cands = get_scrcpy_candidates()
            assert "/opt/homebrew/bin/scrcpy" in mac_cands

    with patch("platform.system", return_value="Windows"):
        with patch("shutil.which", return_value=None):
            win_cands = get_scrcpy_candidates()
            assert r"C:\scrcpy\scrcpy.exe" in win_cands


def test_find_adb_with_custom_valid_path():
    """Проверяет обнаружение adb при явном указании существующего пути."""
    with patch("os.path.isfile", return_value=True):
        with patch("os.access", return_value=True):
            with patch("desktop.binaries.probe_version", return_value="Android Debug Bridge version 1.0.41"):
                info = find_adb("/my/adb")
                assert info.found is True
                assert info.path == "/my/adb"
                assert "1.0.41" in (info.version or "")


def test_find_adb_not_found():
    """Проверяет корректное сообщение об ошибке, если adb нигде не найден."""
    with patch("desktop.binaries.get_adb_candidates", return_value=["/non/existent/adb"]):
        with patch("os.path.isfile", return_value=False):
            info = find_adb()
            assert info.found is False
            assert info.path is None
            assert "не найдена" in (info.error or "")


def test_find_scrcpy_not_found():
    """Проверяет поведение при отсутствии scrcpy."""
    with patch("desktop.binaries.get_scrcpy_candidates", return_value=[]):
        info = find_scrcpy()
        assert info.found is False
        assert "не найдена" in (info.error or "")


def test_probe_version_success():
    """Проверяет извлечение версии через вызов процесса."""
    mock_proc = MagicMock()
    mock_proc.stdout = "scrcpy 2.4 <https://github.com/Genymobile/scrcpy>\nDependencies: SDL 2.30"
    mock_proc.stderr = ""
    with patch("subprocess.run", return_value=mock_proc):
        ver = probe_version(["scrcpy", "--version"])
        assert ver is not None
        assert "scrcpy 2.4" in ver


def test_installation_guide_contents():
    """Проверяет наличие инструкций для Mac, Windows и отладки по USB."""
    guide = get_installation_guide()
    assert "macOS" in guide
    assert "Windows" in guide
    assert "brew install" in guide
    assert "winget install" in guide
    assert "~/android-platform-tools/platform-tools/adb" in guide
    assert "Отладка по USB" in guide
    assert "Номер сборки" in guide
