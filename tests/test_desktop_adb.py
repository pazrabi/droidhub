"""Тесты парсера вывода ADB, обнаружения сетевых устройств и безопасного клиента."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from desktop.adb_client import (
    FORBIDDEN_ADB_COMMANDS,
    AdbClient,
    AdbClientError,
    DeviceInfo,
    is_network_serial,
    map_state_to_russian,
    parse_adb_devices_output,
)


def test_is_network_serial():
    """Тестирование надежного определения сетевых и USB серийных номеров."""
    # USB-устройства (буквенно-цифровые аппаратные ID)
    assert not is_network_serial("RFCT1234567")
    assert not is_network_serial("0123456789ABCDEF")
    assert not is_network_serial("ZY224W4D5Q")
    assert not is_network_serial("emulator-5554")
    assert not is_network_serial("FA6A10302010")

    # Сетевые устройства (IP:порт или host:порт)
    assert is_network_serial("192.168.1.100:5555")
    assert is_network_serial("10.0.0.5:5555")
    assert is_network_serial("localhost:5555")
    assert is_network_serial("127.0.0.1:4444")
    assert is_network_serial("phone.tailscale.net:5555")

    # Граничные случаи
    assert not is_network_serial("")
    assert not is_network_serial("abc:")
    assert not is_network_serial(":5555")


def test_map_state_to_russian():
    """Проверка перевода состояний ADB на понятный русский язык."""
    assert map_state_to_russian("device") == "Готов к работе"
    assert "Не авторизован" in map_state_to_russian("unauthorized")
    assert "Офлайн" in map_state_to_russian("offline")
    assert "Авторизация" in map_state_to_russian("authorizing")
    assert "Нет прав доступа" in map_state_to_russian("no permissions")
    assert "Recovery" in map_state_to_russian("recovery")


def test_parse_adb_devices_output_comprehensive():
    """Комплексный тест парсинга вывода adb devices -l с разными типами устройств."""
    sample_output = """* daemon not running; starting it now at tcp:5037
* daemon started successfully
List of devices attached
RFCT1234567            device usb:1-1 product:akita model:Pixel_8a device:akita transport_id:1
192.168.1.50:5555      device product:husky model:Pixel_8_Pro device:husky transport_id:2
ZY224W4D5Q             unauthorized usb:337641472X transport_id:3
0123456789ABCDEF       offline transport_id:4
FA6A10302010           no permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com/tools/device.html] transport_id:5
"""
    devices = parse_adb_devices_output(sample_output)
    assert len(devices) == 5

    # 1. Готовое USB-устройство Pixel 8a
    d1 = devices[0]
    assert d1.serial == "RFCT1234567"
    assert d1.state == "device"
    assert d1.is_ready is True
    assert d1.is_network is False
    assert d1.connection_type == "USB"
    assert d1.model == "Pixel_8a"
    assert d1.model_display == "Pixel 8a"
    assert "Pixel 8a" in d1.display_name
    assert d1.usb_info == "1-1"
    assert d1.transport_id == "1"

    # 2. Сетевое устройство (должно быть помечено как is_network=True)
    d2 = devices[1]
    assert d2.serial == "192.168.1.50:5555"
    assert d2.state == "device"
    assert d2.is_ready is True
    assert d2.is_network is True
    assert "Сетевое" in d2.connection_type
    assert d2.model == "Pixel_8_Pro"
    assert d2.model_display == "Pixel 8 Pro"

    # 3. Неавторизованное USB-устройство
    d3 = devices[2]
    assert d3.serial == "ZY224W4D5Q"
    assert d3.state == "unauthorized"
    assert d3.is_ready is False
    assert d3.is_network is False
    assert "Не авторизован" in d3.state_display

    # 4. Офлайн устройство
    d4 = devices[3]
    assert d4.serial == "0123456789ABCDEF"
    assert d4.state == "offline"
    assert d4.is_ready is False
    assert "Офлайн" in d4.state_display

    # 5. Устройство без прав (Linux udev)
    d5 = devices[4]
    assert d5.serial == "FA6A10302010"
    assert "no permissions" in d5.state
    assert d5.is_ready is False
    assert "Нет прав" in d5.state_display


def test_parse_adb_devices_empty():
    """Проверка парсинга пустого списка устройств."""
    output = "List of devices attached\n\n"
    devices = parse_adb_devices_output(output)
    assert devices == []


def test_adb_client_forbidden_commands():
    """Проверка блокировки небезопасных команд (shell, root, connect, tcpip и др.)."""
    client = AdbClient("/mock/adb")
    for cmd in ["shell", "root", "connect", "tcpip", "install", "push"]:
        with pytest.raises(AdbClientError) as exc_info:
            client._execute([cmd, "arg"])
        assert "запрещена" in str(exc_info.value)


def test_adb_client_list_devices_success():
    """Проверка успешного вызова list_devices с аргументами без shell=True."""
    client = AdbClient("/mock/adb")
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "List of devices attached\nRFCT111 device model:Pixel_7\n"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        devs = client.list_devices()
        assert len(devs) == 1
        assert devs[0].serial == "RFCT111"
        assert devs[0].model == "Pixel_7"
        # Проверяем строгое отсутствие shell=True и список аргументов
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["/mock/adb", "devices", "-l"]
        assert kwargs.get("shell") is False


def test_adb_client_error_exit_code():
    """Проверка обработки ненулевого кода возврата ADB."""
    client = AdbClient("/mock/adb")
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "cannot connect to daemon"

    with patch("subprocess.run", return_value=mock_proc):
        with pytest.raises(AdbClientError) as exc_info:
            client.list_devices()
        assert "код ошибки 1" in str(exc_info.value)
        assert "cannot connect to daemon" in str(exc_info.value)


def test_adb_client_file_not_found():
    """Проверка ошибки отсутствия исполняемого файла ADB."""
    client = AdbClient("/non/existent/adb")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(AdbClientError) as exc_info:
            client.list_devices()
        assert "не найден по пути" in str(exc_info.value)


def test_adb_client_timeout():
    """Проверка обработки таймаута ADB."""
    client = AdbClient("/mock/adb")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["adb"], timeout=5)):
        with pytest.raises(AdbClientError) as exc_info:
            client.list_devices()
        assert "Превышено время ожидания" in str(exc_info.value)
