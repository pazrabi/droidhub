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


def test_ui_start_selected_usb_fallback_selection_no_mutation(tk_root):
    """Проверка, что fallback на выделенную строку не мутирует checked_serials (issue #3)."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            ctrl.start_selected_usb_devices = MagicMock(return_value=(1, ["Запущено окно"]))

            app = DesktopApp(tk_root, controller=ctrl)
            dev = DeviceInfo(
                serial="USB_SEL_ONLY",
                state="device",
                state_display="Готов к работе",
                is_network=False,
                connection_type="USB",
                is_ready=True,
            )
            app._render_devices([dev])
            items = app.tree.get_children()
            assert len(items) == 1
            app.tree.selection_set(items[0])

            # Чекбоксы не отмечены
            assert len(app.checked_serials) == 0

            # Запуск для выделенного устройства
            app.start_selected_usb()

            # Контроллер получил серийник выделенной строки
            ctrl.start_selected_usb_devices.assert_called_once_with(["USB_SEL_ONLY"])

            # КРИТИЧНО: checked_serials НЕ должен мутировать!
            assert len(app.checked_serials) == 0
            assert app.tree.item(items[0], "text") == "[ ]"

            app.on_close()


def test_ui_double_click_heading_and_empty_space_ignored(tk_root):
    """Проверка игнорирования двойных кликов по заголовку и пустой области (issue #4)."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            ctrl.start_device_scrcpy = MagicMock(return_value=(True, None))
            ctrl.stop_device_scrcpy = MagicMock(return_value=(True, None))

            app = DesktopApp(tk_root, controller=ctrl)
            dev = DeviceInfo(
                serial="DEV_CLICK",
                state="device",
                state_display="Готов к работе",
                is_network=False,
                connection_type="USB",
                is_ready=True,
            )
            app._render_devices([dev])
            items = app.tree.get_children()
            app.tree.selection_set(items[0])

            # 1. Клик по заголовку
            event_heading = MagicMock()
            event_heading.x = 10
            event_heading.y = 10
            app.tree.identify_row = MagicMock(return_value="")
            app.tree.identify_region = MagicMock(return_value="heading")

            app._on_tree_double_click(event_heading)
            ctrl.start_device_scrcpy.assert_not_called()
            ctrl.stop_device_scrcpy.assert_not_called()

            # 2. Клик по пустой области ниже строк
            app.tree.identify_row = MagicMock(return_value="")
            app.tree.identify_region = MagicMock(return_value="nothing")

            app._on_tree_double_click(event_heading)
            ctrl.start_device_scrcpy.assert_not_called()
            ctrl.stop_device_scrcpy.assert_not_called()

            # 3. Клик по строке устройства
            app.tree.identify_row = MagicMock(return_value=items[0])
            app.tree.identify_region = MagicMock(return_value="cell")

            app._on_tree_double_click(event_heading)
            ctrl.start_device_scrcpy.assert_called_once_with("DEV_CLICK")

            app.on_close()


def test_ui_guard_against_tcl_error_when_closing(tk_root):
    """Проверка защиты от TclError при отрисовке закрывающегося окна (issue #5)."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            # Симулируем флаг закрытия
            app._is_closing = True

            # Вызовы должны безопасно прерываться без исключений
            app._render_devices([])
            app._show_refresh_error("test error")

            # Симулируем уничтоженное окно
            app._is_closing = False
            app.root = MagicMock()
            app.root.winfo_exists.return_value = False

            app._render_devices([])
            app._show_refresh_error("test error")
