"""Пользовательский интерфейс DroidHub Desktop Controller на базе tkinter (stdlib)."""

from __future__ import annotations

import logging
import platform
import queue
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from desktop.adb_client import DeviceInfo
from desktop.controller import AppController
from desktop.hub_client import HubDevice


logger = logging.getLogger(__name__)


class DesktopApp:
    """Главное окно настольного приложения DroidHub."""

    def __init__(self, root: tk.Tk, controller: AppController | None = None) -> None:
        self.root = root
        self.controller = controller or AppController()

        self.root.title("DroidHub — Настольный контроллер Android (USB)")
        self.root.geometry("920x580")
        self.root.minsize(780, 480)

        # Выбранные серийные номера (чекбоксы)
        self.checked_serials: set[str] = set()

        # Флаг автообновления
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self._is_closing = False
        self._timer_id: str | None = None

        # Очередь и состояние запросов для удалённого сервера DroidHub
        self._hub_queue: queue.Queue = queue.Queue()
        self._current_hub_request_id: int = 0
        self._hub_poll_timer: str | None = None

        self._init_styles()
        self._create_widgets()

        # Протокол закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Первичное обновление
        self.trigger_refresh()
        # Запуск цикла автообновления
        self._schedule_auto_refresh()
        # Запуск цикла опроса очереди хаба в UI-потоке
        self._poll_hub_queue()

    def _init_styles(self) -> None:
        """Настройка стилей интерфейса."""
        style = ttk.Style(self.root)
        # Использование доступной темы
        available_themes = style.theme_names()
        if "clam" in available_themes:
            style.theme_use("clam")

        style.configure("Header.TLabel", font=("Helvetica", 11, "bold"))
        style.configure("Status.TLabel", font=("Helvetica", 9))
        style.configure("Warning.TLabel", font=("Helvetica", 9, "bold"), foreground="#B00020")
        style.configure("Success.TLabel", font=("Helvetica", 9, "bold"), foreground="#007E33")

    def _create_widgets(self) -> None:
        """Создает элементы управления и разметку окна."""
        # 1. Верхняя панель: статус утилит и быстрые действия
        top_frame = ttk.Frame(self.root, padding="10 8 10 4")
        top_frame.pack(fill=tk.X)

        # Индикаторы утилит
        utils_frame = ttk.LabelFrame(top_frame, text=" Внешние утилиты ", padding="6")
        utils_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        self.adb_status_label = ttk.Label(utils_frame, text="ADB: проверка...", style="Status.TLabel")
        self.adb_status_label.pack(side=tk.LEFT, padx=6)

        self.scrcpy_status_label = ttk.Label(utils_frame, text="scrcpy: проверка...", style="Status.TLabel")
        self.scrcpy_status_label.pack(side=tk.LEFT, padx=12)

        self._update_binary_status_labels()

        # Кнопки помощи и настроек
        btns_frame = ttk.Frame(top_frame)
        btns_frame.pack(side=tk.RIGHT)

        help_btn = ttk.Button(btns_frame, text="❓ Справка и настройка", command=self.show_help_dialog)
        help_btn.pack(side=tk.RIGHT, padx=4)

        paths_btn = ttk.Button(btns_frame, text="⚙ Пути к утилитам", command=self.show_paths_dialog)
        paths_btn.pack(side=tk.RIGHT, padx=4)

        # 2. Вкладки приложения: Локальные устройства (USB) и Удалённый сервер DroidHub
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 4))

        self.tab_usb = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_usb, text="📱 Локальные устройства (USB)")

        self.tab_hub = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_hub, text="🌐 Удалённый сервер DroidHub")

        self._create_usb_tab_widgets(self.tab_usb)
        self._create_hub_tab_widgets(self.tab_hub)

        # 3. Нижняя панель состояния
        bottom_frame = ttk.Frame(self.root, relief=tk.SUNKEN, padding="6 4 6 4")
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.summary_label = ttk.Label(bottom_frame, text="Поиск устройств...", style="Status.TLabel")
        self.summary_label.pack(side=tk.LEFT, padx=4)

        self.status_msg_label = ttk.Label(bottom_frame, text="", style="Status.TLabel")
        self.status_msg_label.pack(side=tk.RIGHT, padx=4)

    def _create_usb_tab_widgets(self, parent: ttk.Frame) -> None:
        """Создает элементы управления для вкладки локальных USB-устройств."""
        toolbar = ttk.Frame(parent, padding="0 4 0 4")
        toolbar.pack(fill=tk.X)

        refresh_btn = ttk.Button(toolbar, text="🔄 Обновить", command=self.trigger_refresh)
        refresh_btn.pack(side=tk.LEFT, padx=(0, 6))

        auto_check = ttk.Checkbutton(
            toolbar,
            text="Автообновление (каждые 3 с)",
            variable=self.auto_refresh_var,
        )
        auto_check.pack(side=tk.LEFT, padx=6)

        # Кнопки групповых действий
        start_selected_btn = ttk.Button(
            toolbar,
            text="▶ Открыть scrcpy для выбранных USB",
            command=self.start_selected_usb,
        )
        start_selected_btn.pack(side=tk.LEFT, padx=12)

        stop_all_btn = ttk.Button(
            toolbar,
            text="⏹ Закрыть все окна scrcpy",
            command=self.stop_all_scrcpy,
        )
        stop_all_btn.pack(side=tk.LEFT, padx=4)

        # Таблица обнаруженных устройств
        table_frame = ttk.Frame(parent, padding="0 4 0 4")
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("serial", "model", "type", "state", "scrcpy")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )

        self.tree.heading("#0", text="[✓] Выбор", anchor=tk.W)
        self.tree.heading("serial", text="Серийный номер / ID", anchor=tk.W)
        self.tree.heading("model", text="Модель", anchor=tk.W)
        self.tree.heading("type", text="Тип подключения", anchor=tk.CENTER)
        self.tree.heading("state", text="Состояние ADB", anchor=tk.CENTER)
        self.tree.heading("scrcpy", text="Окно scrcpy", anchor=tk.CENTER)

        self.tree.column("#0", width=80, stretch=False)
        self.tree.column("serial", width=180, stretch=True)
        self.tree.column("model", width=180, stretch=True)
        self.tree.column("type", width=140, stretch=False, anchor=tk.CENTER)
        self.tree.column("state", width=220, stretch=True, anchor=tk.CENTER)
        self.tree.column("scrcpy", width=120, stretch=False, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # События таблицы
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<space>", self._on_tree_space)
        self.tree.bind("<Button-3>", self._on_context_menu)
        if platform.system() == "Darwin":
            self.tree.bind("<Button-2>", self._on_context_menu)

        # Контекстное меню
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="▶ Запустить окно scrcpy", command=self._start_selected_single)
        self.menu.add_command(label="⏹ Закрыть окно scrcpy", command=self._stop_selected_single)
        self.menu.add_separator()
        self.menu.add_command(label="📋 Скопировать серийный номер", command=self._copy_serial)

        # Панель действий для выбранной строки
        row_actions = ttk.Frame(parent, padding="0 4 0 4")
        row_actions.pack(fill=tk.X)

        self.btn_single_start = ttk.Button(
            row_actions,
            text="▶ Запустить scrcpy для выбранного",
            command=self._start_selected_single,
        )
        self.btn_single_start.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_single_stop = ttk.Button(
            row_actions,
            text="⏹ Остановить scrcpy для выбранного",
            command=self._stop_selected_single,
        )
        self.btn_single_stop.pack(side=tk.LEFT, padx=6)

        hint_label = ttk.Label(
            row_actions,
            text="💡 Двойной клик по строке запускает/закрывает окно scrcpy",
            font=("Helvetica", 8, "italic"),
        )
        hint_label.pack(side=tk.RIGHT, padx=6)

    def _create_hub_tab_widgets(self, parent: ttk.Frame) -> None:
        """Создает элементы управления для вкладки удалённого сервера DroidHub."""
        # 1. Панель параметров подключения
        conn_frame = ttk.LabelFrame(parent, text=" Параметры удалённого сервера DroidHub ", padding="10 8 10 8")
        conn_frame.pack(fill=tk.X, padx=0, pady=(6, 4))

        fields_row = ttk.Frame(conn_frame)
        fields_row.pack(fill=tk.X, pady=(2, 6))

        ttk.Label(fields_row, text="URL сервера (HTTPS):").pack(side=tk.LEFT, padx=(0, 4))
        self.hub_url_entry = ttk.Entry(fields_row, width=32)
        self.hub_url_entry.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(fields_row, text="Bearer-токен:").pack(side=tk.LEFT, padx=(0, 4))
        self.hub_token_entry = ttk.Entry(fields_row, width=24, show="*")
        self.hub_token_entry.pack(side=tk.LEFT, padx=(0, 12))

        self.btn_hub_connect = ttk.Button(
            fields_row,
            text="Подключить и обновить",
            command=self.trigger_hub_refresh,
        )
        self.btn_hub_connect.pack(side=tk.LEFT, padx=4)

        notice_label = ttk.Label(
            conn_frame,
            text="🔒 Только просмотр списка устройств (GET /api/devices). Сетевые устройства хаба не являются USB. "
                 "Переключение транспорта, видеосвязь и удалённое управление не поддерживаются.",
            font=("Helvetica", 8, "italic"),
            foreground="#555555",
        )
        notice_label.pack(anchor=tk.W, pady=(2, 0))

        # 2. Таблица устройств удалённого сервера
        table_frame = ttk.Frame(parent, padding="0 4 0 4")
        table_frame.pack(fill=tk.BOTH, expand=True)

        hub_columns = ("name", "online", "battery", "temperature", "screen")
        self.hub_tree = ttk.Treeview(
            table_frame,
            columns=hub_columns,
            show="headings",
            selectmode="browse",
        )

        self.hub_tree.heading("name", text="Имя устройства", anchor=tk.W)
        self.hub_tree.heading("online", text="Онлайн", anchor=tk.CENTER)
        self.hub_tree.heading("battery", text="Заряд", anchor=tk.CENTER)
        self.hub_tree.heading("temperature", text="Температура", anchor=tk.CENTER)
        self.hub_tree.heading("screen", text="Экран", anchor=tk.CENTER)

        self.hub_tree.column("name", width=240, stretch=True)
        self.hub_tree.column("online", width=120, stretch=False, anchor=tk.CENTER)
        self.hub_tree.column("battery", width=140, stretch=False, anchor=tk.CENTER)
        self.hub_tree.column("temperature", width=130, stretch=False, anchor=tk.CENTER)
        self.hub_tree.column("screen", width=130, stretch=False, anchor=tk.CENTER)

        hub_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.hub_tree.yview)
        self.hub_tree.configure(yscrollcommand=hub_scrollbar.set)

        self.hub_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hub_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 3. Нижняя панель состояния вкладки Hub
        hub_bottom = ttk.Frame(parent, padding="0 4 0 2")
        hub_bottom.pack(fill=tk.X)

        self.hub_summary_label = ttk.Label(
            hub_bottom,
            text="Устройств на сервере: —",
            style="Status.TLabel",
        )
        self.hub_summary_label.pack(side=tk.LEFT)

        self.hub_status_msg_label = ttk.Label(
            hub_bottom,
            text="Сервер не подключен. Введите URL и токен.",
            style="Status.TLabel",
        )
        self.hub_status_msg_label.pack(side=tk.RIGHT)

    def _update_binary_status_labels(self) -> None:
        """Обновляет индикаторы наличия утилит adb и scrcpy."""
        adb = self.controller.adb_info
        if adb.found:
            self.adb_status_label.config(
                text=f"✔ ADB найден ({adb.path})",
                style="Success.TLabel",
            )
        else:
            self.adb_status_label.config(
                text="❌ ADB не найден! Нажмите «Справка»",
                style="Warning.TLabel",
            )

        scrcpy = self.controller.scrcpy_info
        if scrcpy.found:
            self.scrcpy_status_label.config(
                text=f"✔ scrcpy найден ({scrcpy.path})",
                style="Success.TLabel",
            )
        else:
            self.scrcpy_status_label.config(
                text="❌ scrcpy не найден! Нажмите «Справка»",
                style="Warning.TLabel",
            )

    def trigger_refresh(self) -> None:
        """Инициирует неблокирующее фоновое обновление списка устройств."""
        self.status_msg_label.config(text="Обновление списка устройств...")

        def on_success(devices: list[DeviceInfo]) -> None:
            if not self._is_closing:
                try:
                    if self.root.winfo_exists():
                        self.root.after(0, self._render_devices, devices)
                except Exception:
                    pass

        def on_error(err: str) -> None:
            if not self._is_closing:
                try:
                    if self.root.winfo_exists():
                        self.root.after(0, self._show_refresh_error, err)
                except Exception:
                    pass

        self.controller.refresh_devices_async(on_success=on_success, on_error=on_error)

    def _render_devices(self, devices: list[DeviceInfo]) -> None:
        """Отрисовывает полученный список устройств в таблице (вызывается в главном потоке)."""
        try:
            if self._is_closing or not self.root.winfo_exists():
                return
        except Exception:
            return

        # Сохраняем текущее выделение
        selected_item = self.tree.selection()
        selected_serial = None
        if selected_item:
            item_data = self.tree.item(selected_item[0])
            if item_data.get("values"):
                selected_serial = str(item_data["values"][0])

        # Очищаем дерево
        for item in self.tree.get_children():
            self.tree.delete(item)

        usb_count = 0
        net_count = 0
        unauthorized_count = 0
        active_scrcpy_count = 0

        # Удаляем из checked_serials те, что больше не подключены
        current_serials = {dev.serial for dev in devices}
        self.checked_serials = {s for s in self.checked_serials if s in current_serials}

        for dev in devices:
            if dev.is_network:
                net_count += 1
            else:
                usb_count += 1

            if dev.state.lower() == "unauthorized":
                unauthorized_count += 1

            is_running = self.controller.scrcpy_manager.is_running(dev.serial)
            if is_running:
                active_scrcpy_count += 1
                scrcpy_status = "🟢 Запущен"
            else:
                scrcpy_status = "⚪ Остановлен"

            is_checked = dev.serial in self.checked_serials
            check_mark = "[✓]" if is_checked else "[ ]"

            item_id = self.tree.insert(
                "",
                tk.END,
                text=check_mark,
                values=(
                    dev.serial,
                    dev.model_display,
                    dev.connection_type,
                    dev.state_display,
                    scrcpy_status,
                ),
            )

            # Восстанавливаем выделение
            if dev.serial == selected_serial:
                self.tree.selection_set(item_id)

        # Обновляем сводку
        summary_text = (
            f"Всего устройств: {len(devices)} (USB: {usb_count}, Сеть: {net_count}) | "
            f"Окон scrcpy: {active_scrcpy_count}"
        )
        self.summary_label.config(text=summary_text)

        if unauthorized_count > 0:
            self.status_msg_label.config(
                text=f"⚠️ {unauthorized_count} устр. требует подтверждения отладки на экране!",
                style="Warning.TLabel",
            )
        else:
            self.status_msg_label.config(text="Готово", style="Status.TLabel")

    def _show_refresh_error(self, err_text: str) -> None:
        """Отображает ошибку обновления устройств."""
        try:
            if self._is_closing or not self.root.winfo_exists():
                return
        except Exception:
            return
        self.status_msg_label.config(text=f"Ошибка: {err_text}", style="Warning.TLabel")

    def _schedule_auto_refresh(self) -> None:
        """Планирует следующее автообновление через 3 секунды."""
        if not self._is_closing:
            if self.auto_refresh_var.get():
                self.trigger_refresh()
            self._timer_id = self.root.after(3000, self._schedule_auto_refresh)

    def _on_tree_click(self, event: tk.Event) -> None:
        """Обрабатывает клик мышью по таблице для переключения чекбокса."""
        region = self.tree.identify("region", event.x, event.y)
        if region == "tree":  # Клик по колонке с чекбоксом (#0)
            item_id = self.tree.identify_row(event.y)
            if item_id:
                self._toggle_check(item_id)

    def _on_tree_space(self, event: tk.Event) -> str:
        """Переключает чекбокс при нажатии клавиши Пробел."""
        selected = self.tree.selection()
        if selected:
            self._toggle_check(selected[0])
        return "break"

    def _toggle_check(self, item_id: str) -> None:
        """Инвертирует состояние чекбокса для строки таблицы."""
        values = self.tree.item(item_id, "values")
        if not values:
            return
        serial = str(values[0])
        if serial in self.checked_serials:
            self.checked_serials.remove(serial)
            self.tree.item(item_id, text="[ ]")
        else:
            self.checked_serials.add(serial)
            self.tree.item(item_id, text="[✓]")

    def _on_tree_double_click(self, event: tk.Event) -> None:
        """Двойной клик запускает scrcpy (если не запущен) или закрывает его."""
        item_id = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not item_id or region == "heading":
            return

        values = self.tree.item(item_id, "values")
        if not values:
            return
        serial = str(values[0])
        if self.controller.scrcpy_manager.is_running(serial):
            self.controller.stop_device_scrcpy(serial)
        else:
            ok, msg = self.controller.start_device_scrcpy(serial)
            if not ok:
                messagebox.showwarning("Запуск scrcpy", msg, parent=self.root)
        self.trigger_refresh()

    def _on_context_menu(self, event: tk.Event) -> None:
        """Открывает контекстное меню по правому клику."""
        item_id = self.tree.identify_row(event.y)
        if item_id:
            self.tree.selection_set(item_id)
            self.menu.post(event.x_root, event.y_root)

    def _get_selected_serial(self) -> str | None:
        """Возвращает серийный номер выделенной строки."""
        selected = self.tree.selection()
        if not selected:
            return None
        values = self.tree.item(selected[0], "values")
        if values:
            return str(values[0])
        return None

    def _start_selected_single(self) -> None:
        """Запускает scrcpy для одного выделенного устройства."""
        serial = self._get_selected_serial()
        if not serial:
            messagebox.showinfo("Запуск scrcpy", "Выберите устройство в таблице.", parent=self.root)
            return
        ok, msg = self.controller.start_device_scrcpy(serial)
        if not ok:
            messagebox.showwarning("Запуск scrcpy", msg, parent=self.root)
        self.trigger_refresh()

    def _stop_selected_single(self) -> None:
        """Останавливает scrcpy для одного выделенного устройства."""
        serial = self._get_selected_serial()
        if not serial:
            messagebox.showinfo("Остановка scrcpy", "Выберите устройство в таблице.", parent=self.root)
            return
        ok, msg = self.controller.stop_device_scrcpy(serial)
        if not ok:
            messagebox.showwarning("Остановка scrcpy", msg, parent=self.root)
        self.trigger_refresh()

    def _copy_serial(self) -> None:
        """Копирует серийный номер выделенного устройства в буфер обмена."""
        serial = self._get_selected_serial()
        if serial:
            self.root.clipboard_clear()
            self.root.clipboard_append(serial)
            self.status_msg_label.config(text=f"Скопировано: {serial}")

    def start_selected_usb(self) -> None:
        """Запускает окна scrcpy для всех отмеченных галочками USB-устройств."""
        serials_to_start: list[str] = []
        if self.checked_serials:
            serials_to_start = list(self.checked_serials)
        else:
            # Если ничего не отмечено галочкой, пробуем взять текущую выделенную строку без изменения checked_serials
            sel = self._get_selected_serial()
            if sel:
                serials_to_start = [sel]
            else:
                messagebox.showinfo(
                    "Запуск scrcpy",
                    "Отметьте галочками [✓] нужные USB-телефоны в таблице.",
                    parent=self.root,
                )
                return

        started, msgs = self.controller.start_selected_usb_devices(serials_to_start)
        msg_text = "\n".join(msgs)
        if started > 0:
            self.status_msg_label.config(text=f"Запущено окон scrcpy: {started}")
        else:
            messagebox.showwarning("Результат запуска scrcpy", msg_text, parent=self.root)
        self.trigger_refresh()

    def stop_all_scrcpy(self) -> None:
        """Закрывает все активные окна scrcpy."""
        stopped = self.controller.stop_all_scrcpy()
        self.status_msg_label.config(text=f"Остановлено окон scrcpy: {stopped}")
        self.trigger_refresh()

    def show_help_dialog(self) -> None:
        """Показывает диалоговое окно со справкой по установке и настройке."""
        help_win = tk.Toplevel(self.root)
        help_win.title("Справка: Установка ADB, scrcpy и настройка USB")
        help_win.geometry("740x540")
        help_win.minsize(600, 400)
        help_win.transient(self.root)

        frame = ttk.Frame(help_win, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)

        txt = tk.Text(frame, wrap=tk.WORD, font=("Courier", 10), padx=8, pady=8)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=txt.yview)
        txt.configure(yscrollcommand=scrollbar.set)

        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        txt.insert("1.0", self.controller.get_instructions())
        txt.configure(state=tk.DISABLED)

        close_btn = ttk.Button(help_win, text="Закрыть", command=help_win.destroy)
        close_btn.pack(pady=8)

    def show_paths_dialog(self) -> None:
        """Диалог ручного указания путей к adb и scrcpy."""
        paths_win = tk.Toplevel(self.root)
        paths_win.title("Настройка путей к утилитам")
        paths_win.geometry("600x220")
        paths_win.resizable(False, False)
        paths_win.transient(self.root)

        frame = ttk.Frame(paths_win, padding="14")
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Путь к исполняемому файлу adb:").grid(row=0, column=0, sticky=tk.W, pady=4)
        adb_entry = ttk.Entry(frame, width=54)
        adb_entry.insert(0, self.controller.adb_info.path or "")
        adb_entry.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        ttk.Label(frame, text="Путь к исполняемому файлу scrcpy:").grid(row=2, column=0, sticky=tk.W, pady=4)
        scrcpy_entry = ttk.Entry(frame, width=54)
        scrcpy_entry.insert(0, self.controller.scrcpy_info.path or "")
        scrcpy_entry.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(0, 14))

        def save_paths() -> None:
            adb_val = adb_entry.get().strip() or None
            scrcpy_val = scrcpy_entry.get().strip() or None
            self.controller.refresh_binaries(custom_adb=adb_val, custom_scrcpy=scrcpy_val)
            self._update_binary_status_labels()
            self.trigger_refresh()
            paths_win.destroy()

        btn_box = ttk.Frame(frame)
        btn_box.grid(row=4, column=0, columnspan=2, sticky=tk.E)

        ttk.Button(btn_box, text="Отмена", command=paths_win.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_box, text="Сохранить и проверить", command=save_paths).pack(side=tk.RIGHT, padx=4)

    def trigger_hub_refresh(self) -> None:
        """Инициирует фоновый неблокирующий запрос к удалённому серверу DroidHub."""
        if self._is_closing:
            return

        url = self.hub_url_entry.get().strip()
        token = self.hub_token_entry.get().strip()

        if not url:
            self._show_hub_error("Укажите URL сервера DroidHub (HTTPS).")
            return
        if not token:
            self._show_hub_error("Укажите токен авторизации (Bearer token).")
            return

        self._current_hub_request_id += 1
        req_id = self._current_hub_request_id

        self.btn_hub_connect.config(state=tk.DISABLED)
        self.hub_status_msg_label.config(
            text="Подключение к серверу DroidHub...",
            style="Status.TLabel",
        )

        try:
            self.controller.fetch_hub_devices_async(
                base_url=url,
                token=token,
                result_queue=self._hub_queue,
                request_id=req_id,
            )
        except Exception as exc:
            self.btn_hub_connect.config(state=tk.NORMAL)
            safe_msg = f"Ошибка запуска запроса: {exc}".replace(token, "***")
            self._show_hub_error(safe_msg)

    def _poll_hub_queue(self) -> None:
        """Периодически опрашивает очередь результатов от фонового потока хаба в потоке UI."""
        if self._is_closing:
            return

        try:
            while not self._hub_queue.empty():
                item = self._hub_queue.get_nowait()
                req_id, devices, err = item

                # Игнорируем устаревшие результаты после нового запроса или закрытия
                if req_id != self._current_hub_request_id or self._is_closing:
                    continue

                self.btn_hub_connect.config(state=tk.NORMAL)
                if err:
                    self._show_hub_error(err)
                else:
                    self._render_hub_devices(devices)
        except queue.Empty:
            pass
        except Exception:
            logger.exception("Исключение при опросе очереди хаба")
        finally:
            if not self._is_closing:
                try:
                    if self.root.winfo_exists():
                        self._hub_poll_timer = self.root.after(100, self._poll_hub_queue)
                except Exception:
                    pass

    def _render_hub_devices(self, devices: list[HubDevice]) -> None:
        """Отрисовывает полученный список устройств хаба в таблице (в главном потоке Tk)."""
        try:
            if self._is_closing or not self.root.winfo_exists():
                return
        except Exception:
            return

        for item in self.hub_tree.get_children():
            self.hub_tree.delete(item)

        online_count = 0
        offline_count = 0

        for dev in devices:
            if dev.online:
                online_count += 1
                online_status = "🟢 Онлайн"
            else:
                offline_count += 1
                online_status = "⚪ Офлайн"

            if dev.battery is not None:
                battery_str = f"{dev.battery}%"
                if dev.charging:
                    battery_str += " (зарядка)"
            else:
                battery_str = "—"

            if dev.temperature is not None:
                temp_str = f"{dev.temperature:.1f} °C"
            else:
                temp_str = "—"

            if dev.screen_on is True:
                screen_str = "Включен"
            elif dev.screen_on is False:
                screen_str = "Выключен"
            else:
                screen_str = "—"

            name_str = dev.name if dev.name else dev.id

            self.hub_tree.insert(
                "",
                tk.END,
                values=(
                    name_str,
                    online_status,
                    battery_str,
                    temp_str,
                    screen_str,
                ),
            )

        self.hub_summary_label.config(
            text=f"Всего устройств на сервере: {len(devices)} (Онлайн: {online_count}, Офлайн: {offline_count})"
        )
        self.hub_status_msg_label.config(text="Данные обновлены", style="Success.TLabel")

    def _show_hub_error(self, err_text: str) -> None:
        """Отображает безопасное сообщение об ошибке хаба."""
        try:
            if self._is_closing or not self.root.winfo_exists():
                return
        except Exception:
            return

        self.btn_hub_connect.config(state=tk.NORMAL)
        self.hub_status_msg_label.config(text=f"Ошибка: {err_text}", style="Warning.TLabel")
        self.hub_summary_label.config(text="Сервер: ошибка подключения")

    def on_close(self) -> None:
        """Безопасное закрытие приложения с остановкой процессов scrcpy и фоновых задач."""
        self._is_closing = True
        if self._timer_id:
            try:
                self.root.after_cancel(self._timer_id)
            except Exception:
                pass
        if self._hub_poll_timer:
            try:
                self.root.after_cancel(self._hub_poll_timer)
            except Exception:
                pass

        try:
            while not self._hub_queue.empty():
                self._hub_queue.get_nowait()
        except Exception:
            pass

        # Останавливаем все открытые окна scrcpy и инвалидируем задачи хаба
        self.controller.shutdown()
        self.root.destroy()
