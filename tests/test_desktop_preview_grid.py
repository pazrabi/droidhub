"""Тесты для сетки предварительного просмотра экранов USB-устройств PreviewGrid."""

from __future__ import annotations

import struct
import tkinter as tk
from typing import Callable, Sequence
from unittest.mock import MagicMock
import zlib

import pytest

from desktop.adb_client import DeviceInfo
from desktop.preview_grid import (
    CARD_SLOT_WIDTH,
    PREVIEW_HEIGHT,
    PREVIEW_WIDTH,
    PreviewCard,
    PreviewGrid,
)


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
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def make_png(w: int = 100, h: int = 200, r: int = 0, g: int = 200, b: int = 100) -> bytes:
    """Генерирует валидный PNG байтовый массив без Pillow."""
    row = b"\x00" + bytes([r, g, b]) * w

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * h))
        + chunk(b"IEND", b"")
    )


def make_device(
    serial: str = "USB_DEV_1",
    is_ready: bool = True,
    state: str = "device",
    model: str = "Pixel 8a",
    is_network: bool = False,
    connection_type: str = "USB",
) -> DeviceInfo:
    """Создает объект DeviceInfo для тестирования."""
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if is_ready else "Не авторизован",
        is_network=is_network,
        connection_type=connection_type,
        is_ready=is_ready,
        model=model,
    )


# ============================================================================
# Тесты контракта и логики, работающие даже без GUI (или с GUI)
# ============================================================================


def test_preview_grid_requires_callable_callback():
    """PreviewGrid требует передачу вызываемого open_devices."""
    with pytest.raises(TypeError, match="open_devices must be callable"):
        PreviewGrid(None, open_devices=None)  # type: ignore

    with pytest.raises(TypeError, match="open_devices must be callable"):
        PreviewGrid(None, open_devices="not_callable")  # type: ignore


def test_is_usb_device_filtering_rules():
    """Проверка статического метода _is_usb_device на отсев сетевых устройств."""
    usb_dev = make_device("SERIAL123", is_network=False, connection_type="USB")
    assert PreviewGrid._is_usb_device(usb_dev) is True

    net_flag_dev = make_device("SERIAL123", is_network=True, connection_type="USB")
    assert PreviewGrid._is_usb_device(net_flag_dev) is False

    net_serial_dev = make_device("192.168.1.50:5555", is_network=False, connection_type="USB")
    assert PreviewGrid._is_usb_device(net_serial_dev) is False

    net_conn_dev = make_device("SERIAL123", is_network=False, connection_type="Сетевое (ADB TCP/IP)")
    assert PreviewGrid._is_usb_device(net_conn_dev) is False


# ============================================================================
# GUI тесты с tk_root
# ============================================================================


def test_initialization_and_empty_state(tk_root):
    """Инициализация PreviewGrid и отображение пустого состояния."""
    opened = []
    grid = PreviewGrid(tk_root, open_devices=lambda s: opened.append(s))
    grid.pack(fill=tk.BOTH, expand=True)

    assert grid.selected_serials() == []
    assert grid._usb_devices == []
    assert len(grid._cards) == 0

    # Проверяем наличие кнопки и списка
    assert grid._open_btn.cget("text") == "Открыть выбранные"
    assert "USB" in grid._left_pane.winfo_children()[0].cget("text")


def test_set_devices_filters_network_and_duplicates(tk_root):
    """set_devices фильтрует сетевые устройства и дубликаты serial."""
    opened = []
    grid = PreviewGrid(tk_root, open_devices=lambda s: opened.append(s))

    dev1 = make_device("USB_1", is_ready=True, model="Pixel 8a")
    dev2 = make_device("USB_2", is_ready=False, state="unauthorized", model="Galaxy S23")
    net1 = make_device("192.168.0.10:5555", is_network=True)
    dup1 = make_device("USB_1", is_ready=True, model="Duplicate")

    grid.set_devices([dev1, net1, dev2, dup1])

    assert len(grid._usb_devices) == 2
    serials = [d.serial for d in grid._usb_devices]
    assert serials == ["USB_1", "USB_2"]
    assert "USB_1" in grid._cards
    assert "USB_2" in grid._cards
    assert "192.168.0.10:5555" not in grid._cards


def test_selection_and_selected_serials(tk_root):
    """Выбор устройств через чекбоксы и метод selected_serials()."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev1 = make_device("USB_1")
    dev2 = make_device("USB_2")
    grid.set_devices([dev1, dev2])

    assert grid.selected_serials() == []

    # Переключаем выбор
    grid._toggle_selection("USB_1")
    assert grid.selected_serials() == ["USB_1"]
    assert grid._cards["USB_1"].is_selected is True
    assert grid._cards["USB_2"].is_selected is False

    grid._toggle_selection("USB_2")
    assert grid.selected_serials() == ["USB_1", "USB_2"]

    grid._toggle_selection("USB_1")
    assert grid.selected_serials() == ["USB_2"]


def test_selection_preserved_on_device_update(tk_root):
    """Выбор сохраняется при обновлении того же снимка устройств."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev1 = make_device("USB_1")
    dev2 = make_device("USB_2")
    grid.set_devices([dev1, dev2])

    grid._toggle_selection("USB_1")
    assert grid.selected_serials() == ["USB_1"]

    # Обновление с теми же устройствами
    grid.set_devices([dev1, dev2])
    assert grid.selected_serials() == ["USB_1"]
    assert grid._cards["USB_1"].is_selected is True

    # Устройство USB_1 отключилось
    grid.set_devices([dev2])
    assert grid.selected_serials() == []
    assert "USB_1" not in grid._cards


def test_open_selected_callback_only_ready_devices(tk_root):
    """Кнопка «Открыть выбранные» вызывает callback только с готовыми USB serial."""
    opened = []
    grid = PreviewGrid(tk_root, open_devices=lambda s: opened.append(s))

    dev_ready = make_device("USB_READY", is_ready=True, state="device")
    dev_unauth = make_device("USB_UNAUTH", is_ready=False, state="unauthorized")
    grid.set_devices([dev_ready, dev_unauth])

    # Выбираем оба устройства
    grid._toggle_selection("USB_READY")
    grid._toggle_selection("USB_UNAUTH")
    assert grid.selected_serials() == ["USB_READY", "USB_UNAUTH"]

    # Нажимаем кнопку «Открыть выбранные»
    grid._open_btn.invoke()
    assert len(opened) == 1
    assert opened[0] == ["USB_READY"]


def test_card_double_click_opens_only_ready_phone(tk_root):
    """Двойной клик по карточке открывает только готовый телефон."""
    opened = []
    grid = PreviewGrid(tk_root, open_devices=lambda s: opened.append(s))

    dev_ready = make_device("READY_PHONE", is_ready=True)
    dev_offline = make_device("OFFLINE_PHONE", is_ready=False, state="offline")
    grid.set_devices([dev_ready, dev_offline])

    # Двойной клик по готовому телефону
    grid._on_card_double_click("READY_PHONE")
    assert opened == [["READY_PHONE"]]

    # Двойной клик по неготовому телефону -> не вызывает callback
    opened.clear()
    grid._on_card_double_click("OFFLINE_PHONE")
    assert opened == []


def test_set_preview_and_preservation(tk_root):
    """Установка кадра и его сохранение при обновлении списка устройств."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev1 = make_device("USB_1", is_ready=True)
    grid.set_devices([dev1])

    png_data = make_png(100, 200, 0, 255, 0)
    grid.set_preview("USB_1", png_data)

    card = grid._cards["USB_1"]
    assert card._photo_ref is not None
    assert grid._previews.get("USB_1") is not None

    # Повторный вызов set_devices с готовым устройством сохраняет кадр
    grid.set_devices([dev1])
    assert card._photo_ref is not None
    assert grid._previews.get("USB_1") is not None


def test_frame_removed_on_loss_of_readiness(tk_root):
    """Кадр удаляется при потере готовности устройства."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev_ready = make_device("USB_1", is_ready=True, state="device")
    grid.set_devices([dev_ready])

    png_data = make_png(100, 200)
    grid.set_preview("USB_1", png_data)
    assert grid._previews.get("USB_1") is not None

    # Устройство потеряло готовность (unauthorized)
    dev_unready = make_device("USB_1", is_ready=False, state="unauthorized")
    grid.set_devices([dev_unready])

    assert grid._previews.get("USB_1") is None
    assert grid._cards["USB_1"]._photo_ref is None
    assert grid._cards["USB_1"]._preview_label.cget("text") == "Не готов"


def test_frame_removed_on_disappearance(tk_root):
    """Кадр и карточка удаляются при исчезновении устройства."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev1 = make_device("USB_1", is_ready=True)
    grid.set_devices([dev1])
    grid.set_preview("USB_1", make_png(100, 200))
    assert "USB_1" in grid._previews

    # Устройство отключилось
    grid.set_devices([])
    assert "USB_1" not in grid._previews
    assert "USB_1" not in grid._cards


def test_set_preview_none_or_invalid_data(tk_root):
    """set_preview с None или невалидными данными очищает кадр без ошибок."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev1 = make_device("USB_1", is_ready=True)
    grid.set_devices([dev1])

    grid.set_preview("USB_1", make_png(100, 200))
    assert grid._previews.get("USB_1") is not None

    # None очищает кадр
    grid.set_preview("USB_1", None)
    assert grid._previews.get("USB_1") is None
    assert grid._cards["USB_1"]._photo_ref is None

    # Невалидные байты не вызывают исключение
    grid.set_preview("USB_1", b"invalid_bytes_not_png")
    assert grid._previews.get("USB_1") is None
    assert grid._cards["USB_1"]._photo_ref is None


def test_aspect_fit_and_fixed_portrait_space(tk_root):
    """Фиксированное портретное место 132x270 и aspect fit для разных ориентаций."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)
    grid.pack()

    dev1 = make_device("PORTRAIT_DEV", is_ready=True)
    dev2 = make_device("LANDSCAPE_DEV", is_ready=True)
    grid.set_devices([dev1, dev2])

    card1 = grid._cards["PORTRAIT_DEV"]
    card2 = grid._cards["LANDSCAPE_DEV"]

    # Проверяем фиксированный размер контейнера предпросмотра
    assert card1._preview_frame.cget("width") == PREVIEW_WIDTH
    assert card1._preview_frame.cget("height") == PREVIEW_HEIGHT
    assert card2._preview_frame.cget("width") == PREVIEW_WIDTH
    assert card2._preview_frame.cget("height") == PREVIEW_HEIGHT

    # Портретный кадр
    portrait_png = make_png(100, 200)
    grid.set_preview("PORTRAIT_DEV", portrait_png)
    assert card1._photo_ref.width() == 100
    assert card1._photo_ref.height() == 200

    # Альбомный кадр (landscape)
    landscape_png = make_png(132, 66)
    grid.set_preview("LANDSCAPE_DEV", landscape_png)
    assert card2._photo_ref.width() == 132
    assert card2._photo_ref.height() == 66

    # Кадр большого размера (превышающий 132x270) автоматически масштабируется subsample
    huge_png = make_png(264, 540)
    grid.set_preview("PORTRAIT_DEV", huge_png)
    assert card1._photo_ref.width() <= PREVIEW_WIDTH
    assert card1._photo_ref.height() <= PREVIEW_HEIGHT


def test_adaptive_grid_columns(tk_root):
    """Ширина адаптирует количество колонок в сетке."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    devs = [make_device(f"USB_{i}") for i in range(6)]
    grid.set_devices(devs)

    # Симулируем событие изменения ширины холста
    class FakeEvent:
        def __init__(self, w):
            self.width = w

    grid._on_canvas_configure(FakeEvent(200))
    assert grid._columns == 1

    grid._on_canvas_configure(FakeEvent(320))
    assert grid._columns == 2

    grid._on_canvas_configure(FakeEvent(500))
    assert grid._columns == 3


def test_card_widget_reuse_avoid_recreation(tk_root):
    """Карточки не пересоздаются при каждом обновлении списка устройств."""
    grid = PreviewGrid(tk_root, open_devices=lambda s: None)

    dev1 = make_device("USB_1", model="Model A")
    grid.set_devices([dev1])
    initial_card = grid._cards["USB_1"]

    # Обновление свойств того же устройства
    dev1_updated = make_device("USB_1", model="Model B")
    grid.set_devices([dev1_updated])

    # Ссылка на экземпляр виджета карточки сохранилась
    assert grid._cards["USB_1"] is initial_card
    assert initial_card._model_label.cget("text") == "Model B"


def test_keyboard_navigation_support(tk_root):
    """Поддержка клавиатуры через список устройств (Space, Return) и кнопку."""
    opened = []
    grid = PreviewGrid(tk_root, open_devices=lambda s: opened.append(s))

    dev1 = make_device("USB_1", is_ready=True)
    dev2 = make_device("USB_2", is_ready=True)
    grid.set_devices([dev1, dev2])

    # Выбираем первый элемент в listbox
    grid._listbox.selection_set(0)
    grid._listbox.activate(0)

    # Нажатие Space переключает чекбокс
    grid._on_listbox_space()
    assert grid.selected_serials() == ["USB_1"]

    grid._on_listbox_space()
    assert grid.selected_serials() == []

    # Нажатие Return открывает готовое устройство
    grid._on_listbox_return()
    assert opened == [["USB_1"]]

    # Нажатие Return на кнопке «Открыть выбранные»
    grid._toggle_selection("USB_2")
    grid._open_btn.invoke()
    assert opened[-1] == ["USB_2"]
