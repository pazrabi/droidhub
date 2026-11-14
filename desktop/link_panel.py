"""Автономная панель Tkinter ttk.Frame для вкладки «Привязки» настольного DroidHub.

Компонент обеспечивает управление явными связями 1:1 между локальными USB Android-устройствами
и удаленными устройствами сервера DroidHub поверх переданного DeviceLinkService.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Sequence

from desktop.adb_client import DeviceInfo
from desktop.device_links import DeviceLinkError
from desktop.hub_client import HubDevice
from desktop.link_service import DeviceLinkService, LinkedPairStatus, LinkServiceError


logger = logging.getLogger(__name__)

HINT_NO_SERVICE = (
    "Сервер DroidHub не подключен. Сначала подключите сервер для работы с привязками устройств."
)


class DeviceLinkPanel(ttk.Frame):
    """Автономный Tkinter ttk.Frame для вкладки «Привязки» настольного DroidHub.

    Публичный интерфейс:
        panel = DeviceLinkPanel(master)
        panel.set_context(
            service=service,
            local_devices=local_devices,
            hub_devices=hub_devices,
        )
    """

    def __init__(
        self,
        master: tk.Misc | None = None,
        *,
        confirm_callback: Callable[[str, str], bool] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        self._confirm_callback = confirm_callback

        self._service: DeviceLinkService | None = None
        self._local_devices: list[DeviceInfo] = []
        self._hub_devices: list[HubDevice] = []

        # Актуальный выбор пользователя
        self._selected_usb_serial: str | None = None
        self._selected_hub_id: str | None = None
        self._selected_pair: tuple[str, str] | None = None

        # Маппинги для выпадающих списков: отображаемая подпись <-> идентификатор
        self._usb_label_to_serial: dict[str, str] = {}
        self._usb_serial_to_label: dict[str, str] = {}
        self._hub_label_to_id: dict[str, str] = {}
        self._hub_id_to_label: dict[str, str] = {}

        # Отображаемые данные пар
        self._rendered_pairs: list[LinkedPairStatus] = []
        self._rendered_pair_iids: set[str] = set()

        # Тексты статусов и ошибок
        self._hint_text: str = ""
        self._error_text: str = ""
        self._link_button_enabled: bool = False
        self._unlink_button_enabled: bool = False

        self._init_styles()
        self._create_widgets()

        # Первичное состояние: контекст отсутствует
        self._update_ui()

    def _init_styles(self) -> None:
        """Безопасная инициализация стилей оформления."""
        try:
            style = ttk.Style(self)
            style.configure("LinkWarning.TLabel", font=("Helvetica", 9, "bold"), foreground="#B00020")
            style.configure("LinkHint.TLabel", font=("Helvetica", 9), foreground="#555555")
            style.configure("LinkHeader.TLabel", font=("Helvetica", 10, "bold"))
        except Exception:
            pass

    def _create_widgets(self) -> None:
        """Создает элементы управления и разметку панели."""
        # 1. Верхний информационный блок статуса и ошибок
        self._status_frame = ttk.Frame(self, padding="4")
        self._status_frame.pack(fill=tk.X, side=tk.TOP)

        self._hint_label = ttk.Label(
            self._status_frame,
            text="",
            style="LinkHint.TLabel",
            wraplength=650,
        )
        self._hint_label.pack(fill=tk.X, padx=4, pady=2)

        self._error_label = ttk.Label(
            self._status_frame,
            text="",
            style="LinkWarning.TLabel",
            wraplength=650,
        )
        self._error_label.pack(fill=tk.X, padx=4, pady=2)

        # 2. Секция создания новой привязки (1:1)
        self._create_frame = ttk.LabelFrame(
            self,
            text=" Создание новой привязки (1:1) ",
            padding="10",
        )
        self._create_frame.pack(fill=tk.X, side=tk.TOP, padx=6, pady=4)

        # Выбор локального USB-устройства
        usb_frame = ttk.Frame(self._create_frame)
        usb_frame.pack(fill=tk.X, pady=3)
        self._usb_title_label = ttk.Label(
            usb_frame, text="Готовое USB-устройство:", width=26, anchor=tk.W
        )
        self._usb_title_label.pack(side=tk.LEFT, padx=(0, 8))
        self._usb_combo = ttk.Combobox(usb_frame, state="disabled")
        self._usb_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._usb_combo.bind("<<ComboboxSelected>>", self._on_usb_selected)

        # Выбор записи хаба
        hub_frame = ttk.Frame(self._create_frame)
        hub_frame.pack(fill=tk.X, pady=3)
        self._hub_title_label = ttk.Label(
            hub_frame, text="Устройство DroidHub:", width=26, anchor=tk.W
        )
        self._hub_title_label.pack(side=tk.LEFT, padx=(0, 8))
        self._hub_combo = ttk.Combobox(hub_frame, state="disabled")
        self._hub_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._hub_combo.bind("<<ComboboxSelected>>", self._on_hub_selected)

        # Кнопка привязки
        action_row = ttk.Frame(self._create_frame)
        action_row.pack(fill=tk.X, pady=(6, 2))
        self._link_button = ttk.Button(
            action_row,
            text="Привязать",
            command=self._on_link_click,
            state="disabled",
        )
        self._link_button.pack(side=tk.RIGHT)

        # 3. Секция сохранённых привязок (таблица и удаление)
        self._table_frame = ttk.LabelFrame(
            self,
            text=" Сохранённые привязки устройств ",
            padding="10",
        )
        self._table_frame.pack(fill=tk.BOTH, expand=True, side=tk.TOP, padx=6, pady=4)

        # Панель действий со списком
        toolbar = ttk.Frame(self._table_frame)
        toolbar.pack(fill=tk.X, side=tk.TOP, pady=(0, 6))

        self._unlink_button = ttk.Button(
            toolbar,
            text="Удалить привязку",
            command=self._on_unlink_click,
            state="disabled",
        )
        self._unlink_button.pack(side=tk.LEFT)

        # Таблица Treeview
        tree_container = ttk.Frame(self._table_frame)
        tree_container.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        columns = ("serial", "usb_model", "hub_id", "hub_name", "status")
        self._tree = ttk.Treeview(
            tree_container,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self._tree.heading("serial", text="USB Serial")
        self._tree.heading("usb_model", text="Модель USB")
        self._tree.heading("hub_id", text="ID хаба")
        self._tree.heading("hub_name", text="Имя в хабе")
        self._tree.heading("status", text="Статус")

        self._tree.column("serial", width=140, anchor=tk.W)
        self._tree.column("usb_model", width=140, anchor=tk.W)
        self._tree.column("hub_id", width=140, anchor=tk.W)
        self._tree.column("hub_name", width=140, anchor=tk.W)
        self._tree.column("status", width=280, anchor=tk.W)

        scrollbar = ttk.Scrollbar(tree_container, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.bind("<<TreeviewSelect>>", self._on_pair_selected)

    @staticmethod
    def format_usb_label(device: DeviceInfo) -> str:
        """Формирует понятную подпись для USB-устройства: модель/имя и серийный номер."""
        model = device.model_display or device.model or "USB"
        return f"{model} ({device.serial})"

    @staticmethod
    def format_hub_label(hub_dev: HubDevice) -> str:
        """Формирует понятную подпись для устройства хаба: имя/модель и ID."""
        name = hub_dev.name or "DroidHub"
        if name == hub_dev.id:
            return f"DroidHub ({hub_dev.id})"
        return f"{name} ({hub_dev.id})"

    def set_context(
        self,
        *,
        service: DeviceLinkService | None,
        local_devices: Sequence[DeviceInfo],
        hub_devices: Sequence[HubDevice],
    ) -> None:
        """Обновляет снимок контекста панели привязок.

        Вызывается главным UI-потоком при каждом обновлении снимка:
        - service: созданный сервис для подключенного сервера или None.
        - local_devices: актуальный список локальных устройств.
        - hub_devices: актуальный список устройств сервера DroidHub.
        """
        service_changed = service is not self._service
        self._service = service
        self._local_devices = list(local_devices)
        self._hub_devices = list(hub_devices)

        if service_changed:
            self._selected_usb_serial = None
            self._selected_hub_id = None
            self._selected_pair = None
            self._clear_error()

        self._update_ui()

    def _update_ui(self) -> None:
        """Обновляет состояние всех элементов интерфейса на основе текущего контекста."""
        if self._service is None:
            self._show_hint(HINT_NO_SERVICE)
            self._clear_create_controls(disabled=True)
            self._clear_pairs_table()
            self._selected_usb_serial = None
            self._selected_hub_id = None
            self._selected_pair = None
            self._update_link_button_state()
            self._update_unlink_button_state()
            return

        self._hide_hint()

        # Запрос доступных элементов и сохранённых связей из текущего сервиса
        try:
            avail_usb = self._service.get_available_usb_devices(self._local_devices)
            avail_hub = self._service.get_available_hub_devices(self._hub_devices)
            linked_pairs = self._service.get_linked_pairs(self._local_devices, self._hub_devices)
        except Exception as exc:
            logger.warning("Ошибка чтения реестра через DeviceLinkService: %s", exc)
            self._show_error(f"Ошибка реестра привязок: {exc}")
            return

        self._clear_error()

        # 1. Обновление доступных USB-устройств
        self._usb_label_to_serial.clear()
        self._usb_serial_to_label.clear()
        for dev in avail_usb:
            label = self.format_usb_label(dev)
            self._usb_label_to_serial[label] = dev.serial
            self._usb_serial_to_label[dev.serial] = label

        usb_values = list(self._usb_label_to_serial.keys())
        try:
            self._usb_combo.configure(values=usb_values)
        except Exception:
            pass

        # Сохранение выбора USB или сброс, если выбранный объект исчез
        if self._selected_usb_serial and self._selected_usb_serial in self._usb_serial_to_label:
            try:
                self._usb_combo.set(self._usb_serial_to_label[self._selected_usb_serial])
            except Exception:
                pass
        else:
            self._selected_usb_serial = None
            try:
                self._usb_combo.set("")
            except Exception:
                pass

        try:
            self._usb_combo.configure(state="readonly" if usb_values else "disabled")
        except Exception:
            pass

        # 2. Обновление доступных устройств DroidHub
        self._hub_label_to_id.clear()
        self._hub_id_to_label.clear()
        for hdev in avail_hub:
            label = self.format_hub_label(hdev)
            self._hub_label_to_id[label] = hdev.id
            self._hub_id_to_label[hdev.id] = label

        hub_values = list(self._hub_label_to_id.keys())
        try:
            self._hub_combo.configure(values=hub_values)
        except Exception:
            pass

        # Сохранение выбора хаба или сброс, если выбранный объект исчез
        if self._selected_hub_id and self._selected_hub_id in self._hub_id_to_label:
            try:
                self._hub_combo.set(self._hub_id_to_label[self._selected_hub_id])
            except Exception:
                pass
        else:
            self._selected_hub_id = None
            try:
                self._hub_combo.set("")
            except Exception:
                pass

        try:
            self._hub_combo.configure(state="readonly" if hub_values else "disabled")
        except Exception:
            pass

        self._update_link_button_state()

        # 3. Обновление таблицы сохранённых пар
        pair_still_exists = False
        if self._selected_pair is not None:
            sel_s, sel_h = self._selected_pair
            pair_still_exists = any(p.serial == sel_s and p.hub_id == sel_h for p in linked_pairs)

        if not pair_still_exists:
            self._selected_pair = None

        new_iids = set()
        for pair in linked_pairs:
            iid = f"{pair.serial}::{pair.hub_id}"
            new_iids.add(iid)

            usb_model = "—"
            if pair.local_device and pair.local_device.model_display:
                usb_model = pair.local_device.model_display
            elif pair.local_present:
                usb_model = "Локальное устройство"
            else:
                usb_model = "— (отключен)"

            hub_name = "—"
            if pair.hub_device and pair.hub_device.name:
                hub_name = pair.hub_device.name
            elif pair.hub_present:
                hub_name = "DroidHub"
            else:
                hub_name = "— (отсутствует)"

            status_str = pair.status_summary or "Сохранено"
            row_values = (pair.serial, usb_model, pair.hub_id, hub_name, status_str)

            if iid in self._rendered_pair_iids:
                try:
                    self._tree.item(iid, values=row_values)
                except Exception:
                    pass
            else:
                try:
                    self._tree.insert("", tk.END, iid=iid, values=row_values)
                except Exception:
                    pass

        # Удаление строк, которых больше нет
        for old_iid in self._rendered_pair_iids - new_iids:
            try:
                self._tree.delete(old_iid)
            except Exception:
                pass

        self._rendered_pairs = list(linked_pairs)
        self._rendered_pair_iids = new_iids

        # Восстановление выбора пары
        if self._selected_pair is not None:
            sel_iid = f"{self._selected_pair[0]}::{self._selected_pair[1]}"
            try:
                self._tree.selection_set(sel_iid)
                self._tree.focus(sel_iid)
            except Exception:
                pass
        else:
            try:
                self._tree.selection_remove(self._tree.selection())
            except Exception:
                pass

        self._update_unlink_button_state()

    def _clear_create_controls(self, *, disabled: bool = True) -> None:
        """Очищает выпадающие списки создания пары."""
        self._usb_label_to_serial.clear()
        self._usb_serial_to_label.clear()
        self._hub_label_to_id.clear()
        self._hub_id_to_label.clear()
        try:
            self._usb_combo.configure(values=[], state="disabled" if disabled else "readonly")
            self._usb_combo.set("")
            self._hub_combo.configure(values=[], state="disabled" if disabled else "readonly")
            self._hub_combo.set("")
        except Exception:
            pass

    def _clear_pairs_table(self) -> None:
        """Очищает список отображаемых привязок."""
        for iid in list(self._rendered_pair_iids):
            try:
                self._tree.delete(iid)
            except Exception:
                pass
        self._rendered_pairs.clear()
        self._rendered_pair_iids.clear()
        try:
            self._tree.selection_remove(self._tree.selection())
        except Exception:
            pass

    def _update_link_button_state(self) -> None:
        """Обновляет доступность кнопки «Привязать»."""
        self._link_button_enabled = bool(
            self._service is not None
            and self._selected_usb_serial
            and self._selected_hub_id
        )
        try:
            self._link_button.configure(state="normal" if self._link_button_enabled else "disabled")
        except Exception:
            pass

    def _update_unlink_button_state(self) -> None:
        """Обновляет доступность кнопки «Удалить привязку»."""
        self._unlink_button_enabled = bool(
            self._service is not None
            and self._selected_pair is not None
        )
        try:
            self._unlink_button.configure(
                state="normal" if self._unlink_button_enabled else "disabled"
            )
        except Exception:
            pass

    def _on_usb_selected(self, event: Any = None) -> None:
        val = self._usb_combo.get()
        if isinstance(val, str) and val in self._usb_label_to_serial:
            self._selected_usb_serial = self._usb_label_to_serial[val]
        elif not val:
            self._selected_usb_serial = None
        self._update_link_button_state()

    def _on_hub_selected(self, event: Any = None) -> None:
        val = self._hub_combo.get()
        if isinstance(val, str) and val in self._hub_label_to_id:
            self._selected_hub_id = self._hub_label_to_id[val]
        elif not val:
            self._selected_hub_id = None
        self._update_link_button_state()

    def _on_pair_selected(self, event: Any = None) -> None:
        sel = self._tree.selection()
        if isinstance(sel, (tuple, list)) and sel:
            iid = str(sel[0])
            if "::" in iid:
                s, h = iid.split("::", 1)
                self._selected_pair = (s, h)
                self._update_unlink_button_state()
                return
        self._selected_pair = None
        self._update_unlink_button_state()

    def select_usb(self, serial: str | None) -> None:
        """Явно выбирает USB-устройство по идентификатору (serial)."""
        if serial is None or serial not in self._usb_serial_to_label:
            self._selected_usb_serial = None
            try:
                self._usb_combo.set("")
            except Exception:
                pass
        else:
            self._selected_usb_serial = serial
            try:
                self._usb_combo.set(self._usb_serial_to_label[serial])
            except Exception:
                pass
        self._update_link_button_state()

    def select_hub(self, hub_id: str | None) -> None:
        """Явно выбирает устройство DroidHub по идентификатору (hub_id)."""
        if hub_id is None or hub_id not in self._hub_id_to_label:
            self._selected_hub_id = None
            try:
                self._hub_combo.set("")
            except Exception:
                pass
        else:
            self._selected_hub_id = hub_id
            try:
                self._hub_combo.set(self._hub_id_to_label[hub_id])
            except Exception:
                pass
        self._update_link_button_state()

    def select_pair(self, serial: str | None, hub_id: str | None = None) -> None:
        """Явно выбирает сохранённую пару по serial и hub_id (или сбрасывает выбор)."""
        if serial is None:
            self._selected_pair = None
            try:
                self._tree.selection_remove(self._tree.selection())
            except Exception:
                pass
            self._update_unlink_button_state()
            return

        matched: tuple[str, str] | None = None
        for p in self._rendered_pairs:
            if p.serial == serial and (hub_id is None or p.hub_id == hub_id):
                matched = (p.serial, p.hub_id)
                break

        if matched is not None:
            self._selected_pair = matched
            iid = f"{matched[0]}::{matched[1]}"
            try:
                self._tree.selection_set(iid)
                self._tree.focus(iid)
            except Exception:
                pass
            self._update_unlink_button_state()
        else:
            self._selected_pair = None
            try:
                self._tree.selection_remove(self._tree.selection())
            except Exception:
                pass
            self._update_unlink_button_state()

    def _on_link_click(self) -> None:
        """Обработчик нажатия на кнопку «Привязать»."""
        if self._service is None:
            self._show_error("Сервер DroidHub не подключен")
            return

        if not self._selected_usb_serial or not self._selected_hub_id:
            self._show_error("Выберите USB-устройство и устройство DroidHub для привязки")
            return

        try:
            self._service.link(
                serial=self._selected_usb_serial,
                hub_id=self._selected_hub_id,
                local_devices=self._local_devices,
                hub_devices=self._hub_devices,
            )
            self._selected_usb_serial = None
            self._selected_hub_id = None
            self._clear_error()
            self._update_ui()
        except (LinkServiceError, DeviceLinkError) as exc:
            self._show_error(str(exc))
        except Exception as exc:
            self._show_error(f"Не удалось создать привязку: {exc}")

    def _confirm_delete(self, serial: str, hub_id: str) -> bool:
        """Запрашивает подтверждение удаления у пользователя."""
        if self._confirm_callback is not None:
            return self._confirm_callback(serial, hub_id)
        try:
            return bool(
                messagebox.askyesno(
                    "Подтверждение удаления",
                    f"Вы действительно хотите удалить привязку устройства?\n\n"
                    f"USB Serial: {serial}\n"
                    f"DroidHub ID: {hub_id}\n\n"
                    f"Действие нельзя отменить.",
                    parent=self,
                )
            )
        except Exception:
            return True

    def _on_unlink_click(self) -> None:
        """Обработчик нажатия на кнопку «Удалить привязку»."""
        if self._service is None:
            self._show_error("Сервер DroidHub не подключен")
            return

        if self._selected_pair is None:
            self._show_error("Выберите сохранённую пару для удаления")
            return

        serial, hub_id = self._selected_pair
        if not self._confirm_delete(serial, hub_id):
            return

        try:
            self._service.unlink(serial=serial, hub_id=hub_id)
            self._selected_pair = None
            self._clear_error()
            self._update_ui()
        except (LinkServiceError, DeviceLinkError) as exc:
            self._show_error(str(exc))
        except Exception as exc:
            self._show_error(f"Не удалось удалить привязку: {exc}")

    def link_selected_devices(self) -> bool:
        """Публичный метод для выполнения привязки выбранных устройств."""
        self._on_link_click()
        return self._error_text == ""

    def unlink_selected_pair(self) -> bool:
        """Публичный метод для удаления выбранной привязанной пары."""
        self._on_unlink_click()
        return self._error_text == ""

    def _show_error(self, message: str) -> None:
        self._error_text = message
        try:
            self._error_label.configure(text=message)
        except Exception:
            pass

    def _clear_error(self) -> None:
        self._error_text = ""
        try:
            self._error_label.configure(text="")
        except Exception:
            pass

    def _show_hint(self, message: str) -> None:
        self._hint_text = message
        try:
            self._hint_label.configure(text=message)
        except Exception:
            pass

    def _hide_hint(self) -> None:
        self._hint_text = ""
        try:
            self._hint_label.configure(text="")
        except Exception:
            pass

    @property
    def selected_usb_serial(self) -> str | None:
        """Текущий выбранный USB serial (или None)."""
        return self._selected_usb_serial

    @property
    def selected_hub_id(self) -> str | None:
        """Текущий выбранный ID устройства хаба (или None)."""
        return self._selected_hub_id

    @property
    def selected_pair(self) -> tuple[str, str] | None:
        """Текущая выбранная сохранённая пара (serial, hub_id) или None."""
        return self._selected_pair

    @property
    def is_link_enabled(self) -> bool:
        """Доступна ли операция привязки."""
        return self._link_button_enabled

    @property
    def is_unlink_enabled(self) -> bool:
        """Доступна ли операция удаления привязки."""
        return self._unlink_button_enabled

    @property
    def current_error(self) -> str:
        """Текущий текст ошибки в интерфейсе."""
        return self._error_text

    @property
    def current_hint(self) -> str:
        """Текущий текст подсказки/статуса подключения."""
        return self._hint_text

    @property
    def rendered_pairs(self) -> list[LinkedPairStatus]:
        """Текущий список отображаемых пар."""
        return list(self._rendered_pairs)


__all__ = ["DeviceLinkPanel"]
