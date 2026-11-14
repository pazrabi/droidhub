"""Тесты жизненного цикла процессов scrcpy, отсутствия дублей и безопасного закрытия."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from desktop.scrcpy_manager import ScrcpyManager, ScrcpySession


def test_scrcpy_session_is_running():
    """Проверка метода is_running для сессии scrcpy."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Процесс активен
    session = ScrcpySession(
        serial="USB123",
        process=mock_proc,
        start_time=time.time(),
        window_title="DroidHub: Test",
    )
    assert session.is_running() is True
    assert session.exit_code is None

    mock_proc.poll.return_value = 0  # Процесс завершился
    assert session.is_running() is False
    assert session.exit_code == 0


def test_scrcpy_manager_start_success():
    """Проверка успешного запуска scrcpy: аргументы списком, явный -s, shell=False."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Успешно работает

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        with patch("time.sleep"):  # Не ждем в тесте
            ok, err = manager.start("RFCT999", title="Pixel 8")
            assert ok is True
            assert err is None
            assert manager.is_running("RFCT999") is True

            # Проверяем параметры вызова Popen
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            cmd = args[0]
            assert cmd[0] == "/mock/scrcpy"
            assert "-s" in cmd
            assert cmd[cmd.index("-s") + 1] == "RFCT999"
            assert "--window-title=DroidHub: Pixel 8" in cmd
            assert kwargs.get("shell") is False
            assert kwargs.get("encoding") == "utf-8"
            assert kwargs.get("errors") == "replace"


def test_scrcpy_manager_prevent_duplicate_launch():
    """Проверка защиты от повторного запуска scrcpy для одного и того же устройства."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        with patch("time.sleep"):
            # Первый запуск успешен
            ok1, _ = manager.start("RFCT999")
            assert ok1 is True
            assert mock_popen.call_count == 1

            # Второй запуск для того же серийника должен быть отклонен без вызова Popen!
            ok2, err2 = manager.start("RFCT999")
            assert ok2 is False
            assert "уже запущено" in (err2 or "")
            assert mock_popen.call_count == 1  # Больше Popen не вызывался!


def test_scrcpy_manager_immediate_crash_handling():
    """Проверка обработки немедленного падения scrcpy при старте."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # Упал с кодом 1
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = "ERROR: Could not open display"

    with patch("subprocess.Popen", return_value=mock_proc):
        with patch("time.sleep"):
            ok, err = manager.start("RFCT999")
            assert ok is False
            assert "Could not open display" in (err or "")
            assert manager.is_running("RFCT999") is False


def test_scrcpy_manager_file_not_found():
    """Проверка ошибки отсутствия исполняемого файла scrcpy."""
    manager = ScrcpyManager("/non/existent/scrcpy")
    with patch("subprocess.Popen", side_effect=FileNotFoundError):
        ok, err = manager.start("RFCT999")
        assert ok is False
        assert "не найден по пути" in (err or "")


def test_scrcpy_manager_stop_graceful_and_force():
    """Проверка мягкой остановки SIGTERM и принудительного SIGKILL при задержке."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("subprocess.Popen", return_value=mock_proc):
        with patch("time.sleep"):
            manager.start("DEV1")

    # Сценарий 1: завершается штатно после terminate
    mock_proc.wait.return_value = 0
    ok, err = manager.stop("DEV1")
    assert ok is True
    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_not_called()

    # Сценарий 2: таймаут на wait -> вызывается kill()
    mock_proc.reset_mock()
    mock_proc.poll.return_value = None
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd=["scrcpy"], timeout=1.5), 0]
    ok, err = manager.stop("DEV1")
    assert ok is True
    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()


def test_scrcpy_manager_stop_all():
    """Проверка закрытия всех открытых процессов."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock1 = MagicMock()
    mock1.poll.return_value = None
    mock2 = MagicMock()
    mock2.poll.return_value = None

    with patch("subprocess.Popen", side_effect=[mock1, mock2]):
        with patch("time.sleep"):
            manager.start("DEV1")
            manager.start("DEV2")

    stopped = manager.stop_all()
    assert stopped == 2
    mock1.terminate.assert_called_once()
    mock2.terminate.assert_called_once()


def test_scrcpy_manager_handle_device_disconnection():
    """Проверка завершения scrcpy при физическом отключении устройства от USB."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock1 = MagicMock()
    mock1.poll.return_value = None
    mock2 = MagicMock()
    mock2.poll.return_value = None

    with patch("subprocess.Popen", side_effect=[mock1, mock2]):
        with patch("time.sleep"):
            manager.start("USB_REMAINS")
            manager.start("USB_DISCONNECTED")

    # Теперь в подключенных остался только USB_REMAINS
    active_now = {"USB_REMAINS"}
    disconnected = manager.handle_device_disconnection(active_now)

    assert "USB_DISCONNECTED" in disconnected
    mock2.terminate.assert_called_once()
    # Для оставшегося terminate не вызывался
    mock1.terminate.assert_not_called()


def test_scrcpy_manager_background_stderr_drain():
    """Проверка фонового чтения stderr в отдельном daemon-потоке без deadlock (issue #1)."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    # Эмулируем построчный поток вывода в stderr
    mock_proc.stderr = ["WARN: frame dropped\n", "INFO: connected\n"]

    with patch("subprocess.Popen", return_value=mock_proc):
        with patch("time.sleep"):
            ok, err = manager.start("DEV_DRAIN")
            assert ok is True
            session = manager.get_session("DEV_DRAIN")
            assert session is not None
            assert session.reader_thread is not None
            assert session.reader_thread.is_alive() is False  # список завершился быстро
            assert "WARN: frame dropped" in session.stderr_lines
            assert "INFO: connected" in session.stderr_lines


def test_scrcpy_manager_poll_sessions_cleanup_fd_and_no_reread():
    """Проверка закрытия stderr и защиты от повторных чтений завершенных процессов (issue #2)."""
    manager = ScrcpyManager("/mock/scrcpy")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Изначально запущен

    with patch("subprocess.Popen", return_value=mock_proc):
        with patch("time.sleep"):
            manager.start("DEV_POLL")

    session = manager.get_session("DEV_POLL")
    assert session is not None

    # Теперь процесс завершился штатно (код 0)
    mock_proc.poll.return_value = 0
    mock_proc.stderr = MagicMock()

    # Первый опрос: должен закрыть дескриптор и зафиксировать last_error = ""
    status1 = manager.poll_sessions()
    assert status1["DEV_POLL"] is False
    assert session.last_error == ""
    mock_proc.stderr.close.assert_called_once()

    # Сбрасываем счетчик вызовов
    mock_proc.stderr.reset_mock()

    # Второй опрос: повторных чтений или закрытий происходить НЕ должно!
    status2 = manager.poll_sessions()
    assert status2["DEV_POLL"] is False
    mock_proc.stderr.read.assert_not_called()
    mock_proc.stderr.close.assert_not_called()
