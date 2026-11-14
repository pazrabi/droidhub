"""Тесты интерфейса DesktopApp (с поддержкой headless-окружений)."""

from __future__ import annotations

import os
import tkinter as tk
from unittest.mock import MagicMock, patch

import pytest

from desktop.adb_client import DeviceInfo
from desktop.binaries import BinaryInfo
from desktop.controller import AppController
from desktop.ui import DesktopApp


def can_init_tk() -> bool:
    """Проверяет возможность создания реального Tk окна в текущем окружении."""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@pytest.fixture
def tk_root():
    """Фикстура Tk корня для сред с дисплеем."""
    if not can_init_tk():
        pytest.skip("Графический дисплей недоступен в текущей среде (headless)")
    root = tk.Tk()
    root.withdraw()  # Скрываем окно, чтобы не мешать
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def test_ui_initialization_and_rendering(tk_root):
    """Тестирование инициализации DesktopApp и отрисовки элементов интерфейса."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            assert app.root == tk_root
            assert "DroidHub" in tk_root.title()
            assert app.auto_refresh_var.get() is True

            # Тестируем рендеринг устройств
            dev1 = DeviceInfo(
                serial="USB_PHONE",
                state="device",
                state_display="Готов к работе",
                is_network=False,
                connection_type="USB",
                is_ready=True,
                model="Pixel_8a",
            )
            dev2 = DeviceInfo(
                serial="UNAUTH_PHONE",
                state="unauthorized",
                state_display="Не авторизован",
                is_network=False,
                connection_type="USB",
                is_ready=False,
                model="Xiaomi",
            )

            app._render_devices([dev1, dev2])

            items = app.tree.get_children()
            assert len(items) == 2

            # Проверяем переключение чекбокса
            app._toggle_check(items[0])
            assert "USB_PHONE" in app.checked_serials
            assert app.tree.item(items[0], "text") == "[✓]"

            app._toggle_check(items[0])
            assert "USB_PHONE" not in app.checked_serials
            assert app.tree.item(items[0], "text") == "[ ]"

            # Закрытие
            app.on_close()


def test_ui_start_selected_usb_handling(tk_root):
    """Проверка вызова запуска для отмеченных чекбоксами устройств."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            ctrl.start_selected_usb_devices = MagicMock(return_value=(1, ["Запущено окно для Pixel"]))

            app = DesktopApp(tk_root, controller=ctrl)
            app.checked_serials = {"USB_PHONE_1"}

            app.start_selected_usb()
            ctrl.start_selected_usb_devices.assert_called_once_with(["USB_PHONE_1"])
            app.on_close()
