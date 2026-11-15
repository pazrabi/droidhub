"""Компонент сетки предварительного просмотра экранов USB-устройств DroidHub."""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, Sequence

from desktop.adb_client import DeviceInfo, is_network_serial

logger = logging.getLogger(__name__)

# Цветовая палитра для аккуратной тёмной темы
BG_PANEL = "#1e1e1e"
BG_SIDEBAR = "#252526"
BG_CARD = "#252526"
BG_CARD_SELECTED = "#1e293b"
BORDER_CARD = "#3c3c3c"
BORDER_CARD_SELECTED = "#007acc"
BG_PREVIEW = "#141414"
FG_TEXT = "#ffffff"
FG_MUTED = "#888888"
FG_READY = "#4ec9b0"
FG_NOT_READY = "#f48771"
FILTER_LABELS = {"all": "Все", "ready": "Готовы", "not_ready": "Не готовы"}

# Размеры портретной области и слота карточки
PREVIEW_WIDTH = 132
PREVIEW_HEIGHT = 270
CARD_SLOT_WIDTH = 154


class PreviewCard(tk.Frame):
    """Карточка отдельного USB-устройства с экраном предварительного просмотра."""

    def __init__(
        self,
        master: tk.Misc,
        device: DeviceInfo,
        *,
        on_toggle: Callable[[str], None],
        on_double_click: Callable[[str], None],
    ) -> None:
        super().__init__(
            master,
            bg=BG_CARD,
            highlightbackground=BORDER_CARD,
            highlightthickness=1,
            bd=0,
            padx=4,
            pady=4,
        )
        self._device = device
        self._on_toggle = on_toggle
        self._on_double_click = on_double_click
        self._is_selected = False
        self._photo_ref: tk.PhotoImage | None = None

        self._create_widgets()
        self._bind_events()

    @property
    def serial(self) -> str:
        return self._device.serial

    @property
    def device(self) -> DeviceInfo:
        return self._device

    @property
    def is_selected(self) -> bool:
        return self._is_selected

    def _create_widgets(self) -> None:
        # Верхняя панель: чекбокс выбора и статус готовности
        self._header = tk.Frame(self, bg=BG_CARD)
        self._header.pack(fill=tk.X, padx=2, pady=(0, 4))

        self._checkbox = tk.Checkbutton(
            self._header,
            bg=BG_CARD,
            activebackground=BG_CARD,
            selectcolor="#141414",
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            command=self._on_check_clicked,
        )
        self._checkbox.pack(side=tk.LEFT)

        state_text = self._device.state_display or ("Готов" if self._device.is_ready else "Не готов")
        state_fg = FG_READY if self._device.is_ready else FG_NOT_READY
        self._state_label = tk.Label(
            self._header,
            text=state_text,
            font=("Helvetica", 8),
            fg=state_fg,
            bg=BG_CARD,
            anchor="e",
        )
        self._state_label.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        # Портретное место фиксированного размера около 132x270
        self._preview_frame = tk.Frame(
            self,
            width=PREVIEW_WIDTH,
            height=PREVIEW_HEIGHT,
            bg=BG_PREVIEW,
            highlightthickness=0,
            bd=0,
        )
        self._preview_frame.pack_propagate(False)
        self._preview_frame.pack(padx=2, pady=2)

        placeholder_text = "Нет кадра" if self._device.is_ready else "Не готов"
        self._preview_label = tk.Label(
            self._preview_frame,
            bg=BG_PREVIEW,
            fg=FG_MUTED,
            text=placeholder_text,
            font=("Helvetica", 9),
            bd=0,
            highlightthickness=0,
        )
        self._preview_label.place(relx=0.5, rely=0.5, anchor="center")

        # Нижняя панель: подпись модели и серийный номер
        self._footer = tk.Frame(self, bg=BG_CARD)
        self._footer.pack(fill=tk.X, padx=2, pady=(4, 2))

        self._model_label = tk.Label(
            self._footer,
            text=self._device.model_display,
            font=("Helvetica", 9, "bold"),
            fg=FG_TEXT,
            bg=BG_CARD,
            anchor="w",
        )
        self._model_label.pack(fill=tk.X)

        self._serial_label = tk.Label(
            self._footer,
            text=self._device.serial,
            font=("Helvetica", 8),
            fg=FG_MUTED,
            bg=BG_CARD,
            anchor="w",
        )
        self._serial_label.pack(fill=tk.X)

    def _bind_events(self) -> None:
        # Двойной клик по любому элементу карточки (кроме чекбокса)
        for w in (
            self,
            self._header,
            self._state_label,
            self._preview_frame,
            self._preview_label,
            self._footer,
            self._model_label,
            self._serial_label,
        ):
            w.bind("<Double-Button-1>", lambda e: self._on_double_click(self.serial))
            w.config(cursor="hand2")

    def _on_check_clicked(self) -> None:
        self._on_toggle(self.serial)

    def set_selected(self, selected: bool) -> None:
        """Переключает визуальное выделение карточки."""
        self._is_selected = selected
        bg = BG_CARD_SELECTED if selected else BG_CARD
        border = BORDER_CARD_SELECTED if selected else BORDER_CARD
        thickness = 2 if selected else 1

        self.config(bg=bg, highlightbackground=border, highlightthickness=thickness)
        self._header.config(bg=bg)
        self._footer.config(bg=bg)
        self._model_label.config(bg=bg)
        self._serial_label.config(bg=bg)
        self._state_label.config(bg=bg)
        self._checkbox.config(bg=bg, activebackground=bg)

        if selected:
            self._checkbox.select()
        else:
            self._checkbox.deselect()

    def update_device(self, device: DeviceInfo) -> None:
        """Обновляет информацию об устройстве без пересоздания виджета."""
        self._device = device
        state_text = device.state_display or ("Готов" if device.is_ready else "Не готов")
        state_fg = FG_READY if device.is_ready else FG_NOT_READY
        self._state_label.config(text=state_text, fg=state_fg)
        self._model_label.config(text=device.model_display)
        self._serial_label.config(text=device.serial)

        if not device.is_ready or self._photo_ref is None:
            self.clear_preview()

    def set_photo(self, photo: tk.PhotoImage | None) -> None:
        """Устанавливает фото кадра на карточке."""
        self._photo_ref = photo
        if photo is not None:
            self._preview_label.config(image=photo, text="")
        else:
            self.clear_preview()

    def clear_preview(self) -> None:
        """Очищает кадр предпросмотра."""
        self._photo_ref = None
        text = "Нет кадра" if self._device.is_ready else "Не готов"
        self._preview_label.config(image="", text=text)


class PreviewGrid(tk.Frame):
    """Сетка предварительного просмотра USB-экранов DroidHub."""

    def __init__(
        self,
        master: tk.Misc | None = None,
        *,
        open_devices: Callable[[list[str]], None],
        **kwargs,
    ) -> None:
        if not callable(open_devices):
            raise TypeError("open_devices must be callable")

        super().__init__(master, bg=BG_PANEL, **kwargs)

        self._open_devices = open_devices
        self._devices_by_serial: dict[str, DeviceInfo] = {}
        self._usb_devices: list[DeviceInfo] = []
        self._visible_devices: list[DeviceInfo] = []
        self._cards: dict[str, PreviewCard] = {}
        self._selected_serials: set[str] = set()
        self._previews: dict[str, tk.PhotoImage] = {}
        self._columns: int = 1

        self._create_widgets()

    def _create_widgets(self) -> None:
        # Узкий список устройств слева
        self._left_pane = tk.Frame(
            self,
            bg=BG_SIDEBAR,
            width=220,
            highlightthickness=1,
            highlightbackground="#333333",
        )
        self._left_pane.pack_propagate(False)
        self._left_pane.pack(side=tk.LEFT, fill=tk.Y)

        title_lbl = tk.Label(
            self._left_pane,
            text="Устройства USB",
            font=("Helvetica", 10, "bold"),
            fg=FG_TEXT,
            bg=BG_SIDEBAR,
            anchor="w",
        )
        title_lbl.pack(fill=tk.X, padx=10, pady=(10, 4))

        list_frame = tk.Frame(self._left_pane, bg=BG_SIDEBAR)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        list_scroll = ttk.Scrollbar(list_frame, orient="vertical")
        self._listbox = tk.Listbox(
            list_frame,
            bg="#1e1e1e",
            fg="#e0e0e0",
            selectbackground="#094771",
            selectforeground="#ffffff",
            activestyle="none",
            highlightthickness=1,
            highlightbackground="#3c3c3c",
            highlightcolor="#007acc",
            font=("Helvetica", 9),
            exportselection=False,
            takefocus=True,
            yscrollcommand=list_scroll.set,
        )
        list_scroll.config(command=self._listbox.yview)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._listbox.bind("<space>", self._on_listbox_space)
        self._listbox.bind("<Return>", self._on_listbox_return)
        self._listbox.bind("<Double-Button-1>", self._on_listbox_return)
        self._listbox.bind("<Button-1>", self._on_listbox_click)

        self._open_btn = ttk.Button(
            self._left_pane,
            text="Открыть выбранные",
            command=self._on_open_selected,
        )
        self._open_btn.pack(fill=tk.X, padx=8, pady=(4, 10))
        self._open_btn.bind("<Return>", lambda e: self._on_open_selected())

        # Прокручиваемая сетка справа
        self._right_pane = tk.Frame(self, bg=BG_PANEL)
        self._right_pane.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self._create_toolbar()

        self._canvas = tk.Canvas(self._right_pane, bg=BG_PANEL, highlightthickness=0)
        self._canvas_scroll = ttk.Scrollbar(self._right_pane, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._canvas_scroll.set)

        self._canvas_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._cards_container = tk.Frame(self._canvas, bg=BG_PANEL)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._cards_container, anchor="nw")

        self._empty_label = tk.Label(
            self._cards_container,
            text="Нет подключенных USB-устройств",
            bg=BG_PANEL,
            fg=FG_MUTED,
            font=("Helvetica", 11),
        )

        self._cards_container.bind("<Configure>", self._on_cards_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        self._bind_scroll(self._canvas)

    def _bind_scroll(self, widget: tk.Misc) -> None:
        for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            widget.bind(sequence, self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child)

    def _on_mousewheel(self, event: tk.Event) -> None:
        try:
            if event.num == 4:
                self._canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self._canvas.yview_scroll(1, "units")
            elif event.delta:
                steps = max(1, abs(event.delta) // 120)
                self._canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")
        except Exception:
            pass

    def _on_cards_configure(self, event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        cols = max(1, event.width // CARD_SLOT_WIDTH)
        if cols != self._columns:
            self._columns = cols
            self._relayout_cards()

    def _create_toolbar(self) -> None:
        """Создает компактную панель управления над сеткой карточек."""
        self._toolbar = tk.Frame(self._right_pane, bg=BG_PANEL, padx=6, pady=4)
        self._toolbar.pack(side=tk.TOP, fill=tk.X)

        search_lbl = tk.Label(
            self._toolbar,
            text="Поиск",
            font=("Helvetica", 9),
            fg=FG_TEXT,
            bg=BG_PANEL,
        )
        search_lbl.pack(side=tk.LEFT, padx=(2, 4))

        self._search_var = tk.StringVar(self)
        self._search_entry = tk.Entry(
            self._toolbar,
            textvariable=self._search_var,
            bg="#2d2d2d",
            fg=FG_TEXT,
            insertbackground=FG_TEXT,
            bd=1,
            relief="solid",
            highlightthickness=0,
            font=("Helvetica", 9),
            width=14,
        )
        self._search_entry.pack(side=tk.LEFT, padx=(0, 8))
        self._search_var.trace_add("write", self._apply_filters)

        filter_lbl = tk.Label(
            self._toolbar,
            text="Статус",
            font=("Helvetica", 9),
            fg=FG_TEXT,
            bg=BG_PANEL,
        )
        filter_lbl.pack(side=tk.LEFT, padx=(0, 4))

        self._filter_var = tk.StringVar(self, value=FILTER_LABELS["all"])
        self._filter_combo = ttk.Combobox(
            self._toolbar,
            textvariable=self._filter_var,
            values=tuple(FILTER_LABELS.values()),
            state="readonly",
            width=10,
            font=("Helvetica", 9),
        )
        self._filter_combo.pack(side=tk.LEFT, padx=(0, 8))
        self._filter_var.trace_add("write", self._apply_filters)

        self._select_visible_btn = ttk.Button(
            self._toolbar, text="Выбрать видимые", command=self.select_visible,
        )
        self._select_visible_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._deselect_btn = ttk.Button(
            self._toolbar, text="Снять выбор", command=self.deselect_all,
        )
        self._deselect_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._count_label = tk.Label(
            self._toolbar,
            text="Всего: 0",
            font=("Helvetica", 9),
            fg=FG_MUTED,
            bg=BG_PANEL,
            anchor="e",
        )
        self._count_label.pack(side=tk.RIGHT, padx=(4, 4))

    @property
    def visible_devices(self) -> list[DeviceInfo]:
        """Возвращает текущую проекцию видимых устройств."""
        return list(self._visible_devices)

    def set_filter(self, mode: str) -> None:
        """Устанавливает фильтр готовности."""
        self._filter_var.set(FILTER_LABELS[mode])

    def set_search(self, query: str) -> None:
        """Устанавливает поиск по модели или серийному номеру."""
        self._search_var.set(query)

    def _matches_filter(self, dev: DeviceInfo, status: str, query: str) -> bool:
        is_ready = self._is_device_ready(dev)
        if status == FILTER_LABELS["ready"] and not is_ready:
            return False
        if status == FILTER_LABELS["not_ready"] and is_ready:
            return False
        return not query or any(
            query in value.casefold()
            for value in (dev.serial, dev.model_display, dev.model or "", dev.device_code or "")
        )

    def _apply_filters(self, *_: str) -> None:
        status = self._filter_var.get()
        query = self._search_var.get().strip().casefold()
        previous = self._visible_devices
        self._visible_devices = [
            dev for dev in self._usb_devices if self._matches_filter(dev, status, query)
        ]
        self._update_listbox(previous)
        self._relayout_cards()
        total = len(self._usb_devices)
        if query or status != FILTER_LABELS["all"]:
            self._count_label.config(text=f"Показано: {len(self._visible_devices)} из {total}")
        else:
            self._count_label.config(text=f"Всего: {total}")

    def select_visible(self) -> None:
        """Выбирает только текущие видимые готовые телефоны, сохраняя скрытый выбор."""
        self._selected_serials.update(
            dev.serial for dev in self._visible_devices if self._is_device_ready(dev)
        )
        self._sync_cards_selection()
        self._update_listbox()

    def deselect_all(self) -> None:
        """Снимает выбор со всех устройств."""
        self._selected_serials.clear()
        self._sync_cards_selection()
        self._update_listbox()

    def _sync_cards_selection(self) -> None:
        """Синхронизирует визуальное состояние чекбоксов карточек."""
        for serial, card in self._cards.items():
            card.set_selected(serial in self._selected_serials)

    @staticmethod
    def _is_usb_device(dev: DeviceInfo) -> bool:
        """Проверяет, что переданное устройство является аппаратным USB."""
        if getattr(dev, "is_network", False):
            return False
        serial = getattr(dev, "serial", "")
        if not serial or is_network_serial(serial):
            return False
        conn_type = getattr(dev, "connection_type", "") or ""
        if "сетев" in conn_type.lower() or "tcp" in conn_type.lower():
            return False
        return True

    def _is_device_ready(self, serial_or_dev: str | DeviceInfo) -> bool:
        """Проверяет готовность USB-устройства."""
        if isinstance(serial_or_dev, str):
            dev = self._devices_by_serial.get(serial_or_dev)
        else:
            dev = serial_or_dev
        if dev is None:
            return False
        state = getattr(dev, "state", "") or ""
        return bool(getattr(dev, "is_ready", False)) and (state.strip().lower() == "device")

    def set_devices(self, devices: Sequence[DeviceInfo]) -> None:
        """Обновляет список отображаемых устройств.

        Фильтрует только аппаратные USB-устройства.
        Сохраняет выбор и кадры при обновлении того же снимка.
        Убирает кадр при исчезновении устройства или потере готовности.
        Избегает пересоздания карточек при каждом обновлении.
        """
        if devices is None:
            devices = []

        seen_serials = set()
        filtered_usb: list[DeviceInfo] = []
        for dev in devices:
            if self._is_usb_device(dev) and dev.serial not in seen_serials:
                seen_serials.add(dev.serial)
                filtered_usb.append(dev)

        self._usb_devices = filtered_usb
        current_serials = {d.serial for d in filtered_usb}
        new_devices_by_serial = {d.serial: d for d in filtered_usb}

        # 1. Удаление исчезнувших устройств
        old_serials = set(self._devices_by_serial.keys())
        removed_serials = old_serials - current_serials
        for s in removed_serials:
            card = self._cards.pop(s, None)
            if card is not None:
                card.destroy()
            self._previews.pop(s, None)
            self._selected_serials.discard(s)

        self._devices_by_serial = new_devices_by_serial

        # 2. Обновление существующих или создание новых карточек
        for dev in filtered_usb:
            serial = dev.serial
            if serial in self._cards:
                card = self._cards[serial]
                card.update_device(dev)
                if not self._is_device_ready(dev):
                    # Убирай кадр при потере готовности
                    self._previews.pop(serial, None)
                    card.clear_preview()
                elif serial in self._previews:
                    # сохраняй кадр при обновлении того же снимка
                    card.set_photo(self._previews[serial])
            else:
                card = PreviewCard(
                    self._cards_container,
                    dev,
                    on_toggle=self._toggle_selection,
                    on_double_click=self._on_card_double_click,
                )
                self._cards[serial] = card
                self._bind_scroll(card)
                if self._is_device_ready(dev) and serial in self._previews:
                    card.set_photo(self._previews[serial])

            # сохраняй выбор
            card.set_selected(serial in self._selected_serials)

        # 3. Применение фильтров, обновление списка и сетки
        self._apply_filters()

    def set_preview(self, serial: str, png: bytes | None) -> None:
        """Устанавливает кадр предпросмотра для указанного serial.

        Принимает уже уменьшенный PNG или None при недоступности.
        Использует tk.PhotoImage без Pillow.
        Убирает кадр при потере готовности или исчезновении.
        """
        card = self._cards.get(serial)

        if png is None or len(png) == 0:
            self._previews.pop(serial, None)
            if card is not None:
                card.clear_preview()
            return

        # Проверка готовности устройства
        dev = self._devices_by_serial.get(serial)
        if dev is None or not self._is_device_ready(dev):
            self._previews.pop(serial, None)
            if card is not None:
                card.clear_preview()
            return

        try:
            try:
                photo = tk.PhotoImage(master=self, data=png, format="png")
            except Exception:
                photo = tk.PhotoImage(master=self, data=png)

            # Aspect fit: пропорциональное уменьшение, если кадр больше портретного места
            w = photo.width()
            h = photo.height()
            if w > 0 and h > 0 and (w > PREVIEW_WIDTH or h > PREVIEW_HEIGHT):
                scale = max((w + PREVIEW_WIDTH - 1) // PREVIEW_WIDTH, (h + PREVIEW_HEIGHT - 1) // PREVIEW_HEIGHT)
                if scale > 1:
                    photo = photo.subsample(scale, scale)
        except Exception:
            self._previews.pop(serial, None)
            if card is not None:
                card.clear_preview()
            return

        self._previews[serial] = photo
        if card is not None:
            card.set_photo(photo)

    def selected_serials(self) -> list[str]:
        """Возвращает список серийных номеров выбранных USB-устройств."""
        return [d.serial for d in self._usb_devices if d.serial in self._selected_serials]

    def _toggle_selection(self, serial: str) -> None:
        """Переключает выбор устройства по серийному номеру."""
        if serial in self._selected_serials:
            self._selected_serials.remove(serial)
        else:
            self._selected_serials.add(serial)

        card = self._cards.get(serial)
        if card is not None:
            card.set_selected(serial in self._selected_serials)

        self._update_listbox()

    def _on_open_selected(self) -> None:
        """Вызывает callback с выбранными готовыми видимыми USB serial."""
        visible_ready_selected = [
            d.serial for d in self._visible_devices
            if d.serial in self._selected_serials and self._is_device_ready(d)
        ]
        self._open_devices(visible_ready_selected)

    def _on_card_double_click(self, serial: str) -> None:
        """Двойной клик открывает этот готовый телефон."""
        if self._is_device_ready(serial):
            self._open_devices([serial])

    def _on_listbox_space(self, event: tk.Event | None = None) -> str:
        sel = self._listbox.curselection()
        idx = sel[0] if sel else self._listbox.index(tk.ACTIVE)
        if 0 <= idx < len(self._visible_devices):
            self._toggle_selection(self._visible_devices[idx].serial)
        return "break"

    def _on_listbox_return(self, event: tk.Event | None = None) -> str:
        sel = self._listbox.curselection()
        idx = sel[0] if sel else self._listbox.index(tk.ACTIVE)
        if 0 <= idx < len(self._visible_devices):
            dev = self._visible_devices[idx]
            if self._is_device_ready(dev):
                self._open_devices([dev.serial])
        return "break"

    def _on_listbox_click(self, event: tk.Event) -> None:
        idx = self._listbox.nearest(event.y)
        bbox = self._listbox.bbox(idx)
        if bbox and bbox[1] <= event.y <= bbox[1] + bbox[3]:
            if 0 <= idx < len(self._visible_devices):
                self._toggle_selection(self._visible_devices[idx].serial)

    def _update_listbox(self, previous: Sequence[DeviceInfo] | None = None) -> None:
        if previous is None:
            previous = self._visible_devices
        cur_sel = self._listbox.curselection()
        active_idx = self._listbox.index(tk.ACTIVE)
        selected = previous[cur_sel[0]].serial if cur_sel else None
        active = previous[active_idx].serial if active_idx < len(previous) else None

        self._listbox.delete(0, tk.END)
        for index, dev in enumerate(self._visible_devices):
            is_checked = dev.serial in self._selected_serials
            mark = "[✓]" if is_checked else "[ ]"
            state_str = "Готов" if dev.is_ready else dev.state_display
            item_text = f"{mark} {dev.model_display} — {state_str}"
            self._listbox.insert(tk.END, item_text)

            if dev.serial == selected:
                self._listbox.selection_set(index)
            if dev.serial == active:
                self._listbox.activate(index)

    def _relayout_cards(self) -> None:
        visible_serials = {d.serial for d in self._visible_devices}
        for serial, card in self._cards.items():
            if serial not in visible_serials:
                card.grid_forget()

        if not self._visible_devices:
            if not self._usb_devices:
                self._empty_label.config(text="Нет подключенных USB-устройств")
            else:
                self._empty_label.config(text="Нет устройств, соответствующих фильтру")
            self._empty_label.grid(row=0, column=0, padx=20, pady=40, sticky="nsew")
            return

        self._empty_label.grid_forget()
        cols = max(1, self._columns)

        for i, dev in enumerate(self._visible_devices):
            card = self._cards.get(dev.serial)
            if card is not None:
                row = i // cols
                col = i % cols
                card.grid(row=row, column=col, padx=4, pady=4, sticky="n")
