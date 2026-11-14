"""Тесты главного контроллера: неблокирующее обновление, фильтрация сетевых устройств."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from desktop.adb_client import DeviceInfo
from desktop.binaries import BinaryInfo
from desktop.controller import AppController


def make_device(
    serial: str,
    state: str = "device",
    is_network: bool = False,
    model: str = "Pixel",
) -> DeviceInfo:
    """Вспомогательная функция для создания тестового DeviceInfo."""
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if state == "device" else "Не авторизован",
        is_network=is_network,
        connection_type="Сетевое (ADB TCP/IP)" if is_network else "USB",
        is_ready=(state == "device"),
        model=model,
    )


def test_controller_initialization_with_binaries():
    """Проверка инициализации контроллера и обнаружения путей."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb", version="1.0")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy", version="2.0")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            assert ctrl.adb_info.found is True
            assert ctrl.scrcpy_info.found is True
            assert ctrl.adb_client.adb_path == "/bin/adb"
            assert ctrl.scrcpy_manager.scrcpy_path == "/bin/scrcpy"


def test_controller_refresh_sync_success():
    """Проверка синхронного обновления списка устройств."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()

            dev1 = make_device("USB_PHONE_1")
            dev2 = make_device("USB_PHONE_2")
            ctrl.adb_client.list_devices = MagicMock(return_value=[dev1, dev2])

            devices, err = ctrl.refresh_devices_sync()
            assert err is None
            assert len(devices) == 2
            assert ctrl.get_device("USB_PHONE_1") is not None


def test_controller_refresh_sync_no_adb():
    """Проверка сообщения об ошибке при отсутствии ADB."""
    fake_adb = BinaryInfo(name="adb", found=False, error="ADB не найден")
    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=BinaryInfo(name="scrcpy", found=True)):
            ctrl = AppController()
            devices, err = ctrl.refresh_devices_sync()
            assert len(devices) == 0
            assert "ADB не найдена" in (err or "")


def test_controller_refresh_async_non_blocking():
    """Проверка неблокирующего обновления в фоновом потоке."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=BinaryInfo(name="scrcpy", found=True)):
            ctrl = AppController()
            ctrl.adb_client.list_devices = MagicMock(return_value=[make_device("DEV1")])

            results: list[list[DeviceInfo]] = []

            def on_success(devs: list[DeviceInfo]) -> None:
                results.append(devs)

            started = ctrl.refresh_devices_async(on_success=on_success)
            assert started is True

            # Ждем завершения фонового потока
            for _ in range(20):
                if results:
                    break
                time.sleep(0.05)

            assert len(results) == 1
            assert results[0][0].serial == "DEV1"


def test_controller_refuse_unauthorized_and_offline():
    """Контроллер запрещает запуск scrcpy для неавторизованных или офлайн телефонов."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()

            unauth = make_device("UNAUTH_DEV", state="unauthorized")
            offline = make_device("OFFLINE_DEV", state="offline")

            ctrl._devices = [unauth, offline]

            # Попытка запуска для неавторизованного
            ok1, msg1 = ctrl.start_device_scrcpy("UNAUTH_DEV")
            assert ok1 is False
            assert "не авторизовано" in msg1
            assert "Разрешить отладку по USB" in msg1

            # Попытка запуска для офлайн устройства
            ok2, msg2 = ctrl.start_device_scrcpy("OFFLINE_DEV")
            assert ok2 is False
            assert "офлайне" in msg2


def test_controller_start_selected_usb_strictly_skips_network_devices():
    """КРИТИЧЕСКИЙ ТЕСТ: сетевые ADB-устройства никогда не открываются автоматически!"""
    fake_adb = BinaryInfo(name="adb", found=True, path="/bin/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/bin/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()

            usb_ready = make_device("USB_DEV_OK", state="device", is_network=False)
            net_ready = make_device("192.168.1.55:5555", state="device", is_network=True)
            usb_unauth = make_device("USB_UNAUTH", state="unauthorized", is_network=False)

            ctrl._devices = [usb_ready, net_ready, usb_unauth]

            # Мокаем запуск в ScrcpyManager
            ctrl.scrcpy_manager.start = MagicMock(return_value=(True, None))

            # Передаем все 3 серийника в запуск выбранных
            started_count, messages = ctrl.start_selected_usb_devices(
                ["USB_DEV_OK", "192.168.1.55:5555", "USB_UNAUTH"]
            )

            # Только 1 (USB_DEV_OK) должен быть запущен!
            assert started_count == 1
            ctrl.scrcpy_manager.start.assert_called_once_with("USB_DEV_OK", title="Pixel")

            # Проверяем сообщения
            msg_text = " ".join(messages)
            assert "сетевые ADB-устройства не открываются автоматически" in msg_text
            assert "не готов" in msg_text or "статус" in msg_text
            assert "Запущено окно для" in msg_text


def test_controller_shutdown():
    """Проверка корректного закрытия всех процессов при вызове shutdown."""
    ctrl = AppController()
    ctrl.scrcpy_manager.stop_all = MagicMock(return_value=3)
    ctrl.shutdown()
    ctrl.scrcpy_manager.stop_all.assert_called_once()
