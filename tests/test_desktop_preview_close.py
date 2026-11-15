"""Тесты кнопки «Закрыть выбранные» и контракта close_devices в PreviewGrid."""

from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk
from typing import Generator
import pytest

from desktop.adb_client import DeviceInfo
from desktop.preview_grid import PreviewGrid

# 1x1 прозрачный PNG в base64 для тестирования сохранения кадров
SAMPLE_PNG_B64 = b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _is_tk_available() -> bool:
    """Проверяет доступность графического сервера Tk/Xvfb."""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _is_tk_available(),
    reason="Tk/Xvfb display is not available in headless environment",
)


@pytest.fixture
def tk_root() -> Generator[tk.Tk, None, None]:
    """Фикстура изолированного окна Tkinter с гарантированным закрытием."""
    root = tk.Tk()
    root.geometry("800x600")
    try:
        yield root
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _make_device(
    serial: str,
    model: str = "Test Phone",
    is_ready: bool = True,
    state: str = "device",
) -> DeviceInfo:
    """Вспомогательная функция для создания тестового USB-устройства."""
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов" if is_ready else "Не готов",
        is_network=False,
        connection_type="usb",
        is_ready=is_ready,
        model=model,
    )


def test_init_contract_default_close_devices_none(tk_root: tk.Tk) -> None:
    """Проверяет контракт конструктора по умолчанию (close_devices=None):

    Кнопка создается ниже кнопки открытия, подписана «Закрыть выбранные» и отключена.
    """
    open_calls: list[list[str]] = []
    grid = PreviewGrid(tk_root, open_devices=lambda d: open_calls.append(d))
    grid.pack()
    tk_root.update()

    # Проверка существования и надписи кнопки
    assert hasattr(grid, "_close_btn")
    assert grid.close_btn.cget("text") == "Закрыть выбранные"

    # Проверка, что кнопка отключена по умолчанию
    assert str(grid.close_btn.cget("state")) == "disabled"
    assert grid.close_btn.instate(["disabled"])

    # Добавление устройств и попытка передать closeable при отсутствии callback
    dev1 = _make_device("dev1")
    grid.set_devices([dev1])
    grid.select_visible()
    grid.set_closeable_serials({"dev1"})
    tk_root.update()

    # Без callback кнопка обязана оставаться отключенной
    assert str(grid.close_btn.cget("state")) == "disabled"


def test_init_contract_close_devices_type_validation(tk_root: tk.Tk) -> None:
    """Проверяет валидацию типов аргументов конструктора."""
    with pytest.raises(TypeError, match="open_devices must be callable"):
        PreviewGrid(tk_root, open_devices="not_a_callable")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="close_devices must be callable or None"):
        PreviewGrid(tk_root, open_devices=lambda d: None, close_devices="not_a_callable")  # type: ignore[arg-type]


def test_button_disabled_unless_callback_and_visible_checked_closeable(tk_root: tk.Tk) -> None:
    """Проверяет правила доступности кнопки:

    Отключена, если нет callback или нет хотя бы одного видимого выбранного закрываемого серийного номера.
    """
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()
    tk_root.update()

    # 1. Начальное состояние: пусто -> отключена
    assert str(grid.close_btn.cget("state")) == "disabled"

    # 2. Добавлены устройства: dev1, dev2, dev3. Выбора нет, closeable нет -> отключена
    dev1 = _make_device("dev1")
    dev2 = _make_device("dev2")
    dev3 = _make_device("dev3")
    grid.set_devices([dev1, dev2, dev3])
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "disabled"

    # 3. Выбрали dev1, но closeable_serials пустой -> отключена
    grid._toggle_selection("dev1")
    assert str(grid.close_btn.cget("state")) == "disabled"

    # 4. Установили closeable_serials = {"dev2"}, но dev2 не выбран -> отключена
    grid.set_closeable_serials({"dev2"})
    assert str(grid.close_btn.cget("state")) == "disabled"

    # 5. Установили closeable_serials = {"dev1"} (dev1 виден, выбран и closeable) -> ВКЛЮЧЕНА
    grid.set_closeable_serials({"dev1"})
    assert str(grid.close_btn.cget("state")) == "normal"
    assert not grid.close_btn.instate(["disabled"])

    # 6. Сняли выбор с dev1 -> снова отключена
    grid._toggle_selection("dev1")
    assert str(grid.close_btn.cget("state")) == "disabled"

    # 7. Снова выбрали dev1 -> включена
    grid._toggle_selection("dev1")
    assert str(grid.close_btn.cget("state")) == "normal"

    # 8. Сбросили сессии приложения (set_closeable_serials(set())) -> отключена
    grid.set_closeable_serials(set())
    assert str(grid.close_btn.cget("state")) == "disabled"


def test_batch_close_ordered_intersection_one_call(tk_root: tk.Tk) -> None:
    """Проверяет групповой callback:

    Получает упорядоченное пересечение видимых и выбранных с активными/ожидающими,
    ровно один вызов batch callback.
    """
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()

    # Порядок в сетке: dev1, dev2, dev3, dev4
    devs = [_make_device(f"dev{i}") for i in (1, 2, 3, 4)]
    grid.set_devices(devs)

    # Выбраны dev1, dev3, dev4
    grid._toggle_selection("dev1")
    grid._toggle_selection("dev3")
    grid._toggle_selection("dev4")

    # Снимок активных сессий (неупорядоченное множество, с чужим dev99)
    grid.set_closeable_serials({"dev4", "dev3", "dev99"})
    tk_root.update()

    assert str(grid.close_btn.cget("state")) == "normal"

    # Нажимаем кнопку закрытия
    grid.close_btn.invoke()
    tk_root.update()

    # Проверка: ровно 1 вызов, список строго в порядке видимости dev3, dev4
    assert len(close_calls) == 1
    assert close_calls[0] == ["dev3", "dev4"]


def test_preserves_checks_and_images_after_close(tk_root: tk.Tk) -> None:
    """Проверяет сохранение чекбоксов и кадров предпросмотра при закрытии."""
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()

    dev1 = _make_device("dev1")
    dev2 = _make_device("dev2")
    grid.set_devices([dev1, dev2])

    # Устанавливаем кадр на dev1
    grid.set_preview("dev1", SAMPLE_PNG_B64)
    assert "dev1" in grid._previews

    # Выбираем оба устройства
    grid._toggle_selection("dev1")
    grid._toggle_selection("dev2")
    grid.set_closeable_serials({"dev1"})
    tk_root.update()

    # Вызываем закрытие
    grid.close_btn.invoke()
    tk_root.update()

    assert close_calls == [["dev1"]]

    # Чекбоксы сохранены
    assert grid.selected_serials() == ["dev1", "dev2"]
    assert grid._cards["dev1"].is_selected
    assert grid._cards["dev2"].is_selected

    # Кадр предпросмотра сохранен
    assert "dev1" in grid._previews
    assert grid._cards["dev1"]._photo_ref is not None


def test_avoid_rerender_of_cards_on_closeable_state_change(tk_root: tk.Tk) -> None:
    """Проверяет, что вызов set_closeable_serials не пересоздает и не перерисовывает карточки."""
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: None,
    )
    grid.pack()

    dev1 = _make_device("dev1")
    dev2 = _make_device("dev2")
    grid.set_devices([dev1, dev2])
    tk_root.update()

    card1_ref = grid._cards["dev1"]
    card2_ref = grid._cards["dev2"]

    # Меняем состояние закрываемых серий несколько раз
    grid.set_closeable_serials({"dev1"})
    grid.set_closeable_serials({"dev1", "dev2"})
    grid.set_closeable_serials(set())

    # Экземпляры карточек должны остаться абсолютно теми же объектами
    assert grid._cards["dev1"] is card1_ref
    assert grid._cards["dev2"] is card2_ref


def test_filtering_never_sends_filtered_out_serial(tk_root: tk.Tk) -> None:
    """Проверяет, что скрытые фильтрами устройства никогда не отправляются в callback."""
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()

    dev_pixel = _make_device("pixel_1", model="Google Pixel 7")
    dev_samsung = _make_device("samsung_1", model="Samsung Galaxy")
    grid.set_devices([dev_pixel, dev_samsung])

    # Выбираем оба и помечаем оба как закрываемые
    grid.select_visible()
    grid.set_closeable_serials({"pixel_1", "samsung_1"})
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "normal"

    # Фильтруем поиск по "Pixel" -> samsung скрыт
    grid.set_search("Pixel")
    tk_root.update()

    assert [d.serial for d in grid.visible_devices] == ["pixel_1"]

    # Нажимаем закрыть
    grid.close_btn.invoke()
    tk_root.update()

    # samsung_1 не должен попасть в вызов
    assert close_calls == [["pixel_1"]]

    # Фильтруем по запросу, которому ничто не соответствует
    grid.set_search("UnknownDevice")
    tk_root.update()
    assert len(grid.visible_devices) == 0

    # Кнопка закрытия должна стать неактивной
    assert str(grid.close_btn.cget("state")) == "disabled"

    # Очищаем поиск -> кнопка снова активна
    grid.set_search("")
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "normal"


def test_status_filter_interaction(tk_root: tk.Tk) -> None:
    """Проверяет фильтр статуса готовности и его влияние на видимость и закрытие."""
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()

    dev_ready = _make_device("dev_ready", is_ready=True)
    dev_unready = _make_device("dev_unready", is_ready=False, state="offline")
    grid.set_devices([dev_ready, dev_unready])

    # Отмечаем оба через toggle
    grid._toggle_selection("dev_ready")
    grid._toggle_selection("dev_unready")
    grid.set_closeable_serials({"dev_ready", "dev_unready"})
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "normal"

    # Фильтруем только "not_ready"
    grid.set_filter("not_ready")
    tk_root.update()
    assert [d.serial for d in grid.visible_devices] == ["dev_unready"]

    # Закрываем
    grid.close_btn.invoke()
    tk_root.update()
    assert close_calls == [["dev_unready"]]


def test_group_selection_select_visible_and_deselect_all(tk_root: tk.Tk) -> None:
    """Проверяет влияние кнопок «Выбрать видимые» и «Снять выбор» на кнопку закрытия."""
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()

    dev1 = _make_device("dev1", is_ready=True)
    dev2 = _make_device("dev2", is_ready=True)
    grid.set_devices([dev1, dev2])
    grid.set_closeable_serials({"dev1", "dev2"})
    tk_root.update()

    # Пока ничего не выбрано -> disabled
    assert str(grid.close_btn.cget("state")) == "disabled"

    # Выбираем видимые
    grid.select_visible()
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "normal"

    # Снимаем выбор
    grid.deselect_all()
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "disabled"


def test_avoid_duplicate_callbacks_for_return(tk_root: tk.Tk) -> None:
    """Проверяет предотвращение дублирования вызовов при нажатии клавиши Return."""
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()

    dev1 = _make_device("dev1")
    grid.set_devices([dev1])
    grid._toggle_selection("dev1")
    grid.set_closeable_serials({"dev1"})
    tk_root.update()

    # Симулируем обработчик Return на уровне окна (как в диалогах)
    def _on_root_return(e: tk.Event) -> None:
        widget = tk_root.focus_get()
        if widget and hasattr(widget, "invoke"):
            widget.invoke()

    tk_root.bind("<Return>", _on_root_return)

    grid.close_btn.focus_force()
    tk_root.update()

    # Генерируем событие Return на кнопке
    grid.close_btn.event_generate("<Return>")
    tk_root.update()

    # Должен быть строго один вызов
    assert len(close_calls) == 1
    assert close_calls[0] == ["dev1"]

    # Теперь отключаем сессию (кнопка становится disabled)
    grid.set_closeable_serials(set())
    tk_root.update()
    assert str(grid.close_btn.cget("state")) == "disabled"

    # Повторное нажатие Return на отключенной кнопке не должно вызывать callback
    grid.close_btn.event_generate("<Return>")
    tk_root.update()
    assert len(close_calls) == 1


def test_empty_close_never_invokes_callback(tk_root: tk.Tk) -> None:
    """Проверяет, что при отсутствии сессий прямой вызов _on_close_selected безопасен."""
    close_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: None,
        close_devices=lambda d: close_calls.append(d),
    )
    grid.pack()
    tk_root.update()

    grid._on_close_selected()
    assert len(close_calls) == 0


def test_existing_open_devices_contract_preserved(tk_root: tk.Tk) -> None:
    """Проверяет сохранение существующего поведения кнопки открытия."""
    open_calls: list[list[str]] = []
    grid = PreviewGrid(
        tk_root,
        open_devices=lambda d: open_calls.append(d),
        close_devices=lambda d: None,
    )
    grid.pack()

    dev_ready = _make_device("dev_ready", is_ready=True)
    dev_unready = _make_device("dev_unready", is_ready=False, state="offline")
    grid.set_devices([dev_ready, dev_unready])

    grid._toggle_selection("dev_ready")
    grid._toggle_selection("dev_unready")
    tk_root.update()

    # Открытие открывает только готовые выбранные видимые
    grid._open_btn.invoke()
    tk_root.update()
    assert open_calls == [["dev_ready"]]
