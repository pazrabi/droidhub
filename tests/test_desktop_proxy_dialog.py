"""Тесты автономного Tk/ttk диалога USBProxyDialog для HTTP-прокси Android."""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
import tkinter as tk
from typing import Any
from unittest.mock import MagicMock

import pytest

from desktop.proxy_dialog import (
    ProxyActionResult,
    ProxyResult,
    USBProxyDialog,
    _extract_result_fields,
    _proxy_worker,
    setup_clipboard_shortcuts,
)


class ActionRecorder:
    """Простой и потокобезопасный регистратор вызовов для тестов."""

    def __init__(self, return_value: Any = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.return_value = return_value if return_value is not None else []

    def __call__(self, action: str, proxy: str | None) -> Any:
        self.calls.append((action, proxy))
        if isinstance(self.return_value, Exception):
            raise self.return_value
        return self.return_value

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def assert_called_once_with(self, action: str, proxy: str | None) -> None:
        assert self.calls == [(action, proxy)]


@pytest.fixture
def tk_root():
    """Фикстура для корневого окна Tkinter."""
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@dataclass
class CustomResultClass:
    serial: str
    ok: bool
    message: str


def test_proxy_result_protocol_conformance():
    """Проверка протокола ProxyResult и структурной типизации."""
    res1 = ProxyActionResult(serial="RFCT1234", ok=True, message="10.0.0.1:8080")
    assert isinstance(res1, ProxyResult)

    res2 = CustomResultClass(serial="0123456789ABCDEF", ok=False, message="adb error")
    assert isinstance(res2, ProxyResult)

    # Проверка извлечения полей из различных типов объектов
    s, ok, msg = _extract_result_fields(res1)
    assert s == "RFCT1234" and ok is True and msg == "10.0.0.1:8080"

    s, ok, msg = _extract_result_fields({"serial": "dev_dict", "ok": True, "message": "direct"})
    assert s == "dev_dict" and ok is True and msg == "direct"

    s, ok, msg = _extract_result_fields(("dev_tuple", False, "timeout"))
    assert s == "dev_tuple" and ok is False and msg == "timeout"


def test_dialog_ui_elements_and_russian_labels(tk_root):
    """Проверка наличия всех обязательных полей на русском языке: 'HTTP-прокси Android' и 'Адрес и порт'."""
    targets = [("serial_1", "Pixel 6"), ("serial_2", "Galaxy S21")]
    mock_run = ActionRecorder(return_value=[])

    dialog = USBProxyDialog(tk_root, targets, mock_run, auto_read=False)

    # Проверка заголовка окна и заголовка интерфейса
    assert dialog.title() == "HTTP-прокси Android"
    assert "HTTP-прокси Android" in dialog.title_label.cget("text")

    # Проверка метки и поля ввода «Адрес и порт»
    assert dialog.address_label.cget("text") == "Адрес и порт"
    assert dialog.entry.get() == ""

    # Проверка счетчика целевых устройств
    assert dialog.target_count == 2
    assert "Устройств: 2" in dialog.count_label.cget("text")

    # Проверка отображения строк устройств в таблице
    assert dialog.get_status("serial_1") == "—"
    assert dialog.get_status("serial_2") == "—"
    assert dialog.tree.item("serial_1")["values"][0] == "serial_1"
    assert dialog.tree.item("serial_1")["values"][1] == "Pixel 6"
    assert dialog.tree.item("serial_2")["values"][1] == "Galaxy S21"

    # Проверка наличия кнопок действий
    assert dialog.read_button.cget("text") == "Прочитать"
    assert dialog.apply_button.cget("text") == "Применить"
    assert dialog.clear_button.cget("text") == "Сбросить"
    assert dialog.close_button.cget("text") == "Закрыть"

    dialog.destroy()


def test_empty_targets(tk_root):
    """Проверка корректной работы диалога при пустом списке целевых устройств."""
    mock_run = ActionRecorder(return_value=[])
    dialog = USBProxyDialog(tk_root, [], mock_run, auto_read=False)

    assert dialog.target_count == 0
    assert "Устройств: 0" in dialog.count_label.cget("text")
    assert len(dialog.tree.get_children()) == 0

    dialog.destroy()


def test_clipboard_shortcuts(tk_root):
    """Проверка стандартных сочетаний клавиш буфера обмена, включая macOS Command-*."""
    entry = tk.Entry(tk_root)
    entry.pack()
    handlers = setup_clipboard_shortcuts(entry)

    # Проверка привязки клавиш (macOS Command/Meta и Ctrl)
    bound_events = entry.bind()
    for key in ("v", "c", "x", "a"):
        assert any(f"Key-{key}" in b and ("Command" in b or "Meta" in b) for b in bound_events)
        assert any(f"Key-{key}" in b and "Control" in b for b in bound_events)

    # Проверка работы логики обработчиков буфера обмена
    tk_root.clipboard_clear()
    tk_root.clipboard_append("192.168.1.55:8888")

    # Вставка
    handlers["paste"]()
    assert entry.get() == "192.168.1.55:8888"

    # Выделение всего
    handlers["select_all"]()
    assert entry.selection_present()

    # Копирование
    tk_root.clipboard_clear()
    handlers["copy"]()
    assert tk_root.clipboard_get() == "192.168.1.55:8888"

    # Вырезание
    handlers["cut"]()
    assert entry.get() == ""
    assert tk_root.clipboard_get() == "192.168.1.55:8888"


def test_read_action_updates_per_target_rows(tk_root):
    """Проверка действия 'read': опрос через callable в фоновом потоке и обновление строк телефонов."""
    targets = [("dev_1", "Pixel 5"), ("dev_2", "Xiaomi Mi 11")]
    results = [
        ProxyActionResult("dev_1", True, "10.0.0.5:8080"),
        ProxyActionResult("dev_2", False, "устройство офлайн"),
    ]

    mock_run = ActionRecorder(return_value=results)
    dialog = USBProxyDialog(tk_root, targets, mock_run, auto_read=False)

    # Запуск чтения
    dialog.read()
    assert dialog.wait_worker(timeout=2.0) is True

    # Проверка вызова callable
    mock_run.assert_called_once_with("read", None)

    # Проверка индикации текущей настройки и ошибки чтения по каждому телефону
    assert dialog.get_status("dev_1") == "10.0.0.5:8080"
    assert "Ошибка: устройство офлайн" in dialog.get_status("dev_2")
    assert "Завершено с ошибками" in dialog.status_label.cget("text")

    dialog.destroy()


def test_read_action_no_proxy_set(tk_root):
    """Проверка чтения, когда прокси на телефоне не установлен (значения direct / null / :0)."""
    targets = [("dev_1", "Pixel 5")]
    results = [ProxyActionResult("dev_1", True, "")]

    mock_run = ActionRecorder(return_value=results)
    dialog = USBProxyDialog(tk_root, targets, mock_run, auto_read=False)

    dialog.read()
    dialog.wait_worker()

    assert dialog.get_status("dev_1") == "Без прокси"
    assert "Настройки прочитаны" in dialog.status_label.cget("text")

    dialog.destroy()


def test_apply_action_explicit_click(tk_root):
    """Проверка действия 'apply': явный клик применяет указанный прокси."""
    targets = [("dev_1", "Pixel 5"), ("dev_2", "Pixel 6")]
    results = [
        ProxyActionResult("dev_1", True, ""),
        ProxyActionResult("dev_2", True, ""),
    ]

    mock_run = ActionRecorder(return_value=results)
    dialog = USBProxyDialog(tk_root, targets, mock_run, auto_read=False)

    # Попытка применить при пустом поле ввода
    dialog.apply_button.invoke()
    assert mock_run.call_count == 0
    assert "Ошибка: укажите адрес и порт" in dialog.status_label.cget("text")

    # Ввод валидного адреса и явный клик
    dialog.entry.insert(0, "192.168.1.100:3128")
    dialog.apply_button.invoke()
    dialog.wait_worker()

    mock_run.assert_called_once_with("apply", "192.168.1.100:3128")
    assert dialog.get_status("dev_1") == "Установлен"
    assert dialog.get_status("dev_2") == "Установлен"
    assert "Прокси успешно применен" in dialog.status_label.cget("text")

    dialog.destroy()


def test_clear_action_explicit_click(tk_root):
    """Проверка действия 'clear': явный клик по кнопке сброса сбрасывает настройки прокси."""
    targets = [("dev_1", "Pixel 5")]
    results = [ProxyActionResult("dev_1", True, "")]

    mock_run = ActionRecorder(return_value=results)
    dialog = USBProxyDialog(tk_root, targets, mock_run, auto_read=False)

    # Явный клик «Сбросить»
    dialog.clear_button.invoke()
    dialog.wait_worker()

    mock_run.assert_called_once_with("clear", None)
    assert dialog.get_status("dev_1") == "Без прокси"
    assert "Прокси успешно сброшен" in dialog.status_label.cget("text")

    dialog.destroy()


def test_concurrency_disabled_while_busy(tk_root):
    """Проверка блокировки параллельного выполнения: повторные действия отклоняются пока задача выполняется."""
    started_event = threading.Event()
    release_event = threading.Event()

    def slow_worker(action, proxy):
        started_event.set()
        release_event.wait(timeout=2.0)
        return [ProxyActionResult("dev_1", True, "ok")]

    dialog = USBProxyDialog(tk_root, [("dev_1", "Model 1")], slow_worker, auto_read=False)

    # Запускаем действие
    dialog.read()
    assert started_event.wait(timeout=1.0) is True

    # Пока рабочий поток активен: диалог занят, кнопки отключены
    assert dialog.is_busy is True
    assert str(dialog.apply_button.cget("state")) == "disabled"
    assert str(dialog.clear_button.cget("state")) == "disabled"
    assert str(dialog.read_button.cget("state")) == "disabled"

    # Попытка вызвать другое действие отклоняется
    assert dialog.read() is False
    assert dialog.apply("proxy:8080") is False
    assert dialog.clear() is False

    # Освобождаем рабочий поток и завершаем операцию
    release_event.set()
    dialog.wait_worker()

    assert dialog.is_busy is False
    assert str(dialog.apply_button.cget("state")) == "normal"
    assert str(dialog.clear_button.cget("state")) == "normal"
    assert str(dialog.read_button.cget("state")) == "normal"

    dialog.destroy()


def test_worker_isolation_and_no_tk_calls():
    """Проверка изоляции: фоновая функция не получает self/виджеты и помещает результаты в queue."""
    mock_run = ActionRecorder(return_value=[ProxyActionResult("dev_1", True, "test")])
    result_queue: queue.Queue = queue.Queue()

    _proxy_worker("read", None, mock_run, result_queue)

    status, action, data = result_queue.get_nowait()
    assert status == "success"
    assert action == "read"
    assert data[0].serial == "dev_1"


def test_worker_exception_handling(tk_root):
    """Проверка обработки исключения в callable: ошибка отображается в интерфейсе, поток не ломает Tk."""
    def failing_run(action, proxy):
        raise ConnectionResetError("Сбой связи с сервером ADB")

    dialog = USBProxyDialog(tk_root, [("dev_1", "Pixel")], failing_run, auto_read=False)
    dialog.read()
    dialog.wait_worker()

    assert dialog.is_busy is False
    assert "Сбой связи с сервером ADB" in dialog.get_status("dev_1")
    assert "Ошибка выполнения" in dialog.status_label.cget("text")
    assert str(dialog.apply_button.cget("state")) == "normal"

    dialog.destroy()


def test_cleanup_cancels_polling_on_close(tk_root):
    """Проверка отмены таймеров polling и корректного завершения при закрытии окна."""
    def slow_worker(action, proxy):
        time.sleep(0.05)
        return [ProxyActionResult("dev_1", True, "ok")]

    dialog = USBProxyDialog(tk_root, [("dev_1", "Pixel")], slow_worker, auto_read=False)
    dialog.read()

    assert dialog._poll_timer is not None

    # Закрываем окно
    dialog._on_close()

    assert dialog.is_destroyed is True
    assert dialog._poll_timer is None

    # Обработка очереди после закрытия не вызывает ошибок Tk
    dialog.process_queue()


def test_cleanup_cancels_polling_on_root_destroy(tk_root):
    """Проверка отмены таймеров polling при уничтожении родительского окна tk_root."""
    def slow_worker(action, proxy):
        time.sleep(0.05)
        return [ProxyActionResult("dev_1", True, "ok")]

    dialog = USBProxyDialog(tk_root, [("dev_1", "Pixel")], slow_worker, auto_read=False)
    dialog.read()

    assert dialog._poll_timer is not None

    # Уничтожаем root напрямую
    tk_root.destroy()

    assert dialog.is_destroyed is True
    assert dialog._poll_timer is None
