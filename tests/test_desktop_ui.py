"""Тесты интерфейса DesktopApp (с поддержкой headless-окружений)."""

from __future__ import annotations

import os
import tkinter as tk
from unittest.mock import MagicMock, patch

import pytest

from desktop.adb_client import DeviceInfo
from desktop.binaries import BinaryInfo
from desktop.controller import AppController
from desktop.hub_client import HubDevice
from desktop.ui import DesktopApp


_CAN_INIT_TK: bool | None = None


def can_init_tk() -> bool:
    """Проверяет возможность создания реального Tk окна в текущем окружении."""
    global _CAN_INIT_TK
    if _CAN_INIT_TK is None:
        try:
            root = tk.Tk()
            root.destroy()
            _CAN_INIT_TK = True
        except Exception:
            _CAN_INIT_TK = False
    return _CAN_INIT_TK


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

            ctrl.shutdown()


def test_ui_hub_tab_and_widgets(tk_root):
    """Проверка наличия вкладки удалённого сервера DroidHub и её элементов управления."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            # Проверяем вкладки
            tabs = [app.notebook.tab(i, "text") for i in range(app.notebook.index("end"))]
            assert any("USB" in t for t in tabs)
            assert any("Удалённый сервер" in t for t in tabs)

            # Поля ввода
            assert app.hub_url_entry is not None
            assert app.hub_token_entry is not None
            # Поле токена замаскировано
            assert app.hub_token_entry.cget("show") == "*"

            # Кнопка подключения
            assert app.btn_hub_connect.cget("text") == "Подключить и обновить"

            # Таблица устройств хаба
            columns = app.hub_tree.cget("columns")
            assert "name" in columns
            assert "online" in columns
            assert "battery" in columns
            assert "temperature" in columns
            assert "screen" in columns

            # Проверяем заголовки
            assert app.hub_tree.heading("name")["text"] == "Имя устройства"
            assert app.hub_tree.heading("online")["text"] == "Онлайн"
            assert app.hub_tree.heading("battery")["text"] == "Заряд"
            assert app.hub_tree.heading("temperature")["text"] == "Температура"
            assert app.hub_tree.heading("screen")["text"] == "Экран"

            # При запуске запросы не выполняются без действия пользователя
            assert len(app.hub_tree.get_children()) == 0

            app.on_close()


def test_ui_hub_validation_empty_inputs(tk_root):
    """Проверка валидации: при пустом URL или токене отображается безопасное сообщение."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            ctrl.fetch_hub_devices_async = MagicMock()
            app = DesktopApp(tk_root, controller=ctrl)

            # 1. Пустые оба поля
            app.trigger_hub_refresh()
            ctrl.fetch_hub_devices_async.assert_not_called()
            assert "Укажите URL" in app.hub_status_msg_label.cget("text")

            # 2. Указан URL, пустой токен
            app.hub_url_entry.insert(0, "https://hub.local")
            app.trigger_hub_refresh()
            ctrl.fetch_hub_devices_async.assert_not_called()
            assert "Укажите токен" in app.hub_status_msg_label.cget("text")

            app.on_close()


def test_ui_hub_trigger_refresh_calls_async_and_disables_button(tk_root):
    """Проверка запуска фонового запроса при нажатии кнопки и блокировки кнопки на время запроса."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            ctrl.fetch_hub_devices_async = MagicMock(return_value=1)

            app = DesktopApp(tk_root, controller=ctrl)
            app.hub_url_entry.insert(0, "https://hub.example.com")
            app.hub_token_entry.insert(0, "my-secret-token")

            app.trigger_hub_refresh()

            ctrl.fetch_hub_devices_async.assert_called_once_with(
                base_url="https://hub.example.com",
                token="my-secret-token",
                result_queue=app._hub_queue,
                request_id=1,
            )
            assert str(app.btn_hub_connect.cget("state")) == "disabled"
            assert "Подключение к серверу" in app.hub_status_msg_label.cget("text")

            app.on_close()


def test_ui_hub_queue_poll_success_renders_devices(tk_root):
    """Проверка потокобезопасного обновления UI через очередь с правильным отображением полей."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            dev1 = HubDevice(
                id="dev1",
                name="Pixel 7 Pro",
                online=True,
                battery=85,
                temperature=33.2,
                screen_on=True,
                charging=True,
            )
            dev2 = HubDevice(
                id="dev2",
                name="Pixel 6",
                online=False,
                battery=None,
                temperature=None,
                screen_on=None,
                charging=None,
            )

            app._current_hub_request_id = 42
            app.btn_hub_connect.config(state="disabled")

            # Помещаем результат в очередь
            app._hub_queue.put((42, [dev1, dev2], None))

            # Опрашиваем очередь в главном потоке
            app._poll_hub_queue()

            # Кнопка разблокирована
            assert str(app.btn_hub_connect.cget("state")) == "normal"

            # Таблица отрисована
            items = app.hub_tree.get_children()
            assert len(items) == 2

            vals1 = app.hub_tree.item(items[0], "values")
            assert vals1[0] == "Pixel 7 Pro"
            assert "Онлайн" in str(vals1[1])
            assert "85%" in str(vals1[2]) and "зарядка" in str(vals1[2])
            assert "33.2" in str(vals1[3])
            assert vals1[4] == "Включен"

            vals2 = app.hub_tree.item(items[1], "values")
            assert vals2[0] == "Pixel 6"
            assert "Офлайн" in str(vals2[1])
            assert vals2[2] == "—"
            assert vals2[3] == "—"
            assert vals2[4] == "—"

            assert "Всего устройств на сервере: 2" in app.hub_summary_label.cget("text")
            assert "Данные обновлены" in app.hub_status_msg_label.cget("text")

            app.on_close()


def test_ui_hub_queue_ignores_stale_results(tk_root):
    """Результаты устаревших запросов не отображаются в интерфейсе."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            stale_dev = HubDevice(id="stale_id", name="Stale Device", online=True)
            # В очереди результат запроса #1, но актуальный запрос уже #2
            app._current_hub_request_id = 2
            app._hub_queue.put((1, [stale_dev], None))

            app._poll_hub_queue()

            # В таблице ничего не появилось
            assert len(app.hub_tree.get_children()) == 0

            app.on_close()


def test_ui_hub_queue_ignores_when_closing(tk_root):
    """При закрытии окна очередь безопасно игнорирует входящие данные."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            app._is_closing = True
            app._hub_queue.put((1, [HubDevice(id="d", name="D")], None))

            # Вызов не должен вызывать исключений
            app._poll_hub_queue()
            assert len(app.hub_tree.get_children()) == 0

            app.on_close()


def test_ui_hub_error_does_not_break_usb(tk_root):
    """Сбой удалённого сервера отображает безопасную ошибку и не ломает список и работу USB."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            # Рендерим USB устройство
            usb_dev = DeviceInfo(
                serial="USB_DEV",
                state="device",
                state_display="Готов к работе",
                is_network=False,
                connection_type="USB",
                is_ready=True,
            )
            app._render_devices([usb_dev])
            assert len(app.tree.get_children()) == 1

            # Симулируем ошибку хаба
            app._current_hub_request_id = 5
            app._hub_queue.put((5, [], "Сервер DroidHub временно недоступен (HTTP 503)"))

            app._poll_hub_queue()

            # Ошибка отображается на вкладке хаба
            assert "HTTP 503" in app.hub_status_msg_label.cget("text")
            assert "ошибка" in app.hub_summary_label.cget("text")

            # Список USB устройств остался нетронутым!
            assert len(app.tree.get_children()) == 1
            assert "Готов" in app.status_msg_label.cget("text")

            app.on_close()


def test_ui_hub_error_clears_previous_devices_in_table(tk_root):
    """При ошибке подключения таблица устройств хаба очищается от устаревших данных."""
    fake_adb = BinaryInfo(name="adb", found=True, path="/fake/adb")
    fake_scrcpy = BinaryInfo(name="scrcpy", found=True, path="/fake/scrcpy")

    with patch("desktop.controller.find_adb", return_value=fake_adb):
        with patch("desktop.controller.find_scrcpy", return_value=fake_scrcpy):
            ctrl = AppController()
            app = DesktopApp(tk_root, controller=ctrl)

            # 1. Успешный рендеринг устройств
            prev_dev = HubDevice(id="dev_prev", name="Pixel Prev", online=True, battery=90)
            app._render_hub_devices([prev_dev])
            assert len(app.hub_tree.get_children()) == 1

            # 2. Появление ошибки сервера (например, 401 Unauthorized)
            app._show_hub_error("Ошибка авторизации: 401 Unauthorized")

            # Таблица очищена!
            assert len(app.hub_tree.get_children()) == 0
            assert "401 Unauthorized" in app.hub_status_msg_label.cget("text")
            assert "ошибка" in app.hub_summary_label.cget("text")
            assert str(app.btn_hub_connect.cget("state")) == "normal"

            app.on_close()
