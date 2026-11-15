"""Диалог настройки HTTP-прокси для Android-устройств (ADB/USB)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import queue
from threading import Thread
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ProxyResult(Protocol):
    """Протокол результата операции с прокси для одного устройства."""

    serial: str
    ok: bool
    message: str


@dataclass(frozen=True)
class ProxyActionResult:
    """Конкретная реализация результата операции для использования и тестирования."""

    serial: str
    ok: bool
    message: str


def _extract_result_fields(item: Any) -> tuple[str, bool, str]:
    """Извлекает (serial, ok, message) из структурного объекта, словаря или кортежа."""
    if hasattr(item, "serial") and hasattr(item, "ok") and hasattr(item, "message"):
        return str(item.serial), bool(item.ok), str(item.message)
    if isinstance(item, dict):
        return (
            str(item.get("serial", "")),
            bool(item.get("ok", False)),
            str(item.get("message", "")),
        )
    if isinstance(item, (tuple, list)) and len(item) >= 3:
        return str(item[0]), bool(item[1]), str(item[2])
    return (
        str(getattr(item, "serial", "")),
        bool(getattr(item, "ok", False)),
        str(getattr(item, "message", "")),
    )


def _proxy_worker(
    action: str,
    proxy_value: str | None,
    run_action: Callable[[str, str | None], Sequence[ProxyResult]],
    result_queue: queue.Queue[tuple[str, str, Any]],
) -> None:
    """Фоновый рабочий поток: запускает только переданный callable.

    Не выполняет сетевых вызовов с хоста напрямую, не содержит вызовов Tk
    и не захватывает ссылки на Tcl-виджеты или объект диалога.
    """
    try:
        results = run_action(action, proxy_value)
        result_queue.put(("success", action, list(results)))
    except Exception as exc:
        result_queue.put(("error", action, exc))


def setup_clipboard_shortcuts(entry: tk.Entry | ttk.Entry) -> dict[str, Callable[[], str]]:
    """Настройка стандартных сочетаний клавиш буфера обмена (включая macOS Command-*)."""

    def on_paste(_event: tk.Event | None = None) -> str:
        try:
            text = entry.clipboard_get()
        except tk.TclError:
            return "break"
        if text:
            try:
                if entry.selection_present():
                    entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                pass
            entry.insert(tk.INSERT, text)
        return "break"

    def on_copy(_event: tk.Event | None = None) -> str:
        try:
            if entry.selection_present():
                text = entry.selection_get()
                entry.clipboard_clear()
                entry.clipboard_append(text)
        except tk.TclError:
            pass
        return "break"

    def on_cut(_event: tk.Event | None = None) -> str:
        try:
            if entry.selection_present():
                text = entry.selection_get()
                entry.clipboard_clear()
                entry.clipboard_append(text)
                entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass
        return "break"

    def on_select_all(_event: tk.Event | None = None) -> str:
        entry.select_range(0, tk.END)
        entry.icursor(tk.END)
        return "break"

    handlers = {
        "paste": on_paste,
        "copy": on_copy,
        "cut": on_cut,
        "select_all": on_select_all,
    }
    setattr(entry, "clipboard_handlers", handlers)

    # macOS Command bindings
    for key in ("<Command-v>", "<Command-V>"):
        entry.bind(key, on_paste)
    for key in ("<Command-c>", "<Command-C>"):
        entry.bind(key, on_copy)
    for key in ("<Command-x>", "<Command-X>"):
        entry.bind(key, on_cut)
    for key in ("<Command-a>", "<Command-A>"):
        entry.bind(key, on_select_all)

    # Standard Ctrl bindings
    for key in ("<Control-v>", "<Control-V>"):
        entry.bind(key, on_paste)
    for key in ("<Control-c>", "<Control-C>"):
        entry.bind(key, on_copy)
    for key in ("<Control-x>", "<Control-X>"):
        entry.bind(key, on_cut)
    for key in ("<Control-a>", "<Control-A>"):
        entry.bind(key, on_select_all)

    return handlers


class USBProxyDialog(tk.Toplevel):
    """Автономный диалог настройки HTTP-прокси для подключенных Android-устройств."""

    def __init__(
        self,
        parent: Any,
        targets: Sequence[tuple[str, str]],
        run_action: Callable[[str, str | None], Sequence[ProxyResult]],
        *,
        auto_read: bool = True,
    ) -> None:
        super().__init__(parent)

        self._targets: list[tuple[str, str]] = list(targets)
        self._run_action = run_action
        self._queue: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        self._busy: bool = False
        self._destroyed: bool = False
        self._poll_timer: str | None = None
        self._current_worker: Thread | None = None
        self.target_rows: dict[str, dict[str, Any]] = {}

        self.title("HTTP-прокси Android")
        self.minsize(520, 380)

        if parent is not None and hasattr(parent, "winfo_toplevel"):
            try:
                top = parent.winfo_toplevel()
                if top and top.winfo_exists():
                    self.transient(top)
            except tk.TclError:
                pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Destroy>", self._on_destroy, add="+")

        self._build_ui()
        self._populate_targets()

        # Алиасы для удобства доступа
        self.address_entry = self.entry
        self.reset_button = self.clear_button
        self.reset = self.clear

        if auto_read and self._targets:
            self.read()

    @property
    def targets(self) -> list[tuple[str, str]]:
        return list(self._targets)

    @property
    def target_count(self) -> int:
        return len(self._targets)

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_destroyed(self) -> bool:
        return self._destroyed

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self, padding=(12, 10, 12, 10))
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Заголовок «HTTP-прокси Android»
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 8))
        self.title_label = ttk.Label(
            header_frame,
            text="HTTP-прокси Android",
            font=("", 11, "bold"),
        )
        self.title_label.pack(side=tk.LEFT)

        # Секция ввода «Адрес и порт»
        config_frame = ttk.LabelFrame(main_frame, text="Настройки прокси", padding=(10, 8))
        config_frame.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(config_frame)
        row1.pack(fill=tk.X)

        self.address_label = ttk.Label(row1, text="Адрес и порт")
        self.address_label.pack(side=tk.LEFT, padx=(0, 8))

        self.entry = ttk.Entry(row1, width=28)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        setup_clipboard_shortcuts(self.entry)

        self.paste_button = ttk.Button(row1, text="Вставить", command=self._paste_clipboard)
        self.paste_button.pack(side=tk.LEFT)

        # Секция списка целевых устройств
        devices_frame = ttk.LabelFrame(
            main_frame,
            text=f"Целевые устройства ({len(self._targets)})",
            padding=(10, 8),
        )
        devices_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        info_bar = ttk.Frame(devices_frame)
        info_bar.pack(fill=tk.X, pady=(0, 4))
        self.count_label = ttk.Label(
            info_bar,
            text=f"Устройств: {len(self._targets)}",
        )
        self.count_label.pack(side=tk.LEFT)

        table_frame = ttk.Frame(devices_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("serial", "model", "status"),
            show="headings",
            height=min(8, max(4, len(self._targets))),
        )
        self.tree.heading("serial", text="Серийный номер")
        self.tree.heading("model", text="Модель")
        self.tree.heading("status", text="Текущий прокси")

        self.tree.column("serial", width=140, anchor=tk.W)
        self.tree.column("model", width=140, anchor=tk.W)
        self.tree.column("status", width=200, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Секция индикации прогресса
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=(0, 8))

        self.progressbar = ttk.Progressbar(progress_frame, mode="indeterminate")
        self.progressbar.pack(fill=tk.X, pady=(0, 4))

        self.status_label = ttk.Label(progress_frame, text="Готов к работе")
        self.status_label.pack(anchor=tk.W)

        # Панель кнопок действий
        actions_frame = ttk.Frame(main_frame)
        actions_frame.pack(fill=tk.X)

        self.read_button = ttk.Button(actions_frame, text="Прочитать", command=self.read)
        self.read_button.pack(side=tk.LEFT, padx=(0, 6))

        self.apply_button = ttk.Button(actions_frame, text="Применить", command=self.apply)
        self.apply_button.pack(side=tk.LEFT, padx=(0, 6))

        self.clear_button = ttk.Button(actions_frame, text="Сбросить", command=self.clear)
        self.clear_button.pack(side=tk.LEFT, padx=(0, 6))

        self.close_button = ttk.Button(actions_frame, text="Закрыть", command=self._on_close)
        self.close_button.pack(side=tk.RIGHT)

    def _populate_targets(self) -> None:
        self.target_rows.clear()
        for serial, model in self._targets:
            self.target_rows[serial] = {
                "model": model,
                "status": "—",
                "ok": None,
            }
            self.tree.insert("", tk.END, iid=serial, values=(serial, model, "—"))

    def _paste_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        if text:
            try:
                if self.entry.selection_present():
                    self.entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                pass
            self.entry.insert(tk.INSERT, text.strip())

    def get_status(self, serial: str) -> str:
        """Возвращает текущий отображаемый статус для устройства."""
        if serial in self.target_rows:
            return str(self.target_rows[serial]["status"])
        return ""

    get_target_status = get_status

    def _set_status(self, text: str) -> None:
        if self._destroyed:
            return
        try:
            self.status_label.configure(text=text)
        except tk.TclError:
            pass

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if self._destroyed:
            return
        state = "disabled" if busy else "normal"
        for widget in (
            self.apply_button,
            self.clear_button,
            self.read_button,
            self.entry,
            self.paste_button,
        ):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

        try:
            if busy:
                self.progressbar.start(10)
            else:
                self.progressbar.stop()
        except tk.TclError:
            pass

    def _start_action(self, action: str, proxy_val: str | None = None) -> bool:
        if self._busy:
            logger.warning("Операция '%s' отклонена: предыдущая операция еще не завершена", action)
            return False

        self._set_busy(True)
        if action == "read":
            self._set_status("Чтение текущих настроек прокси...")
        elif action == "apply":
            self._set_status(f"Применение прокси {proxy_val}...")
        elif action == "clear":
            self._set_status("Сброс настроек прокси...")
        else:
            self._set_status(f"Выполнение операции '{action}'...")

        thread = Thread(
            target=_proxy_worker,
            args=(action, proxy_val, self._run_action, self._queue),
            daemon=True,
        )
        self._current_worker = thread
        thread.start()
        self._schedule_poll()
        return True

    def read(self) -> bool:
        """Запуск чтения текущих настроек прокси с целевых телефонов."""
        return self._start_action("read", None)

    read_settings = read

    def apply(self, value: str | None = None) -> bool:
        """Явное применение настроек прокси (хост:порт) на целевые телефоны."""
        if self._busy:
            return False
        proxy_val = value if value is not None else self.entry.get().strip()
        if not proxy_val:
            self._set_status("Ошибка: укажите адрес и порт прокси (хост:порт)")
            return False
        return self._start_action("apply", proxy_val)

    apply_proxy = apply

    def clear(self) -> bool:
        """Явный сброс (очистка) настроек прокси на целевых телефонах."""
        return self._start_action("clear", None)

    clear_proxy = clear

    def _schedule_poll(self) -> None:
        if self._destroyed:
            return
        self._cancel_poll()
        try:
            self._poll_timer = self.after(50, self._poll_queue)
        except (tk.TclError, AttributeError):
            self._poll_timer = None

    def _cancel_poll(self) -> None:
        if self._poll_timer is not None:
            try:
                self.after_cancel(self._poll_timer)
            except (tk.TclError, AttributeError):
                pass
            self._poll_timer = None

    def _poll_queue(self) -> None:
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                self._cleanup()
                return
        except (tk.TclError, AttributeError):
            self._cleanup()
            return

        self.process_queue()

        if self._destroyed:
            return

        if self._busy and self._current_worker and self._current_worker.is_alive():
            self._schedule_poll()
        else:
            self._poll_timer = None

    def process_queue(self) -> None:
        """Синхронная обработка накопившихся в очереди сообщений из рабочего потока."""
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                self._cleanup()
                return
        except (tk.TclError, AttributeError):
            self._cleanup()
            return

        while True:
            try:
                status, action, data = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_worker_result(status, action, data)

    def _format_target_message(self, action: str, ok: bool, message: str) -> str:
        msg = message.strip()
        if not ok:
            return f"Ошибка: {msg}" if msg else "Ошибка"

        if action == "read":
            if not msg or msg.lower() in (":0", "null", "none", "direct"):
                return "Без прокси"
            return msg

        if action == "clear":
            if not msg or msg.lower() in (":0", "null", "none", "direct"):
                return "Без прокси"
            return msg

        if action == "apply":
            return msg if msg else "Установлен"

        return msg or "ОК"

    def _update_target_row(self, serial: str, status_text: str, ok: bool | None) -> None:
        if serial in self.target_rows:
            self.target_rows[serial]["status"] = status_text
            self.target_rows[serial]["ok"] = ok
        try:
            if self.tree.exists(serial):
                self.tree.set(serial, "status", status_text)
        except tk.TclError:
            pass

    def _handle_worker_result(self, status: str, action: str, data: Any) -> None:
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                self._cleanup()
                return
        except (tk.TclError, AttributeError):
            self._cleanup()
            return

        self._set_busy(False)
        self._current_worker = None

        if status == "error":
            err_msg = f"Ошибка выполнения: {data}"
            self._set_status(err_msg)
            for serial in self.target_rows:
                self._update_target_row(serial, f"Ошибка: {data}", ok=False)
            return

        results = data if isinstance(data, list) else []
        success_count = 0
        total_count = len(results)

        for item in results:
            serial, ok, msg = _extract_result_fields(item)
            formatted = self._format_target_message(action, ok, msg)
            self._update_target_row(serial, formatted, ok=ok)
            if ok:
                success_count += 1

        if total_count == 0:
            self._set_status("Готово: нет устройств для обработки")
        elif success_count == total_count:
            if action == "read":
                self._set_status(f"Настройки прочитаны ({success_count} из {total_count})")
            elif action == "apply":
                self._set_status(f"Прокси успешно применен ({success_count} из {total_count})")
            elif action == "clear":
                self._set_status(f"Прокси успешно сброшен ({success_count} из {total_count})")
            else:
                self._set_status(f"Успешно: {success_count} из {total_count}")
        else:
            self._set_status(
                f"Завершено с ошибками (успешно {success_count} из {total_count})"
            )

    def wait_worker(self, timeout: float = 2.0) -> bool:
        """Ожидание завершения фонового потока и синхронная обработка очереди результатов.

        Позволяет тестам быстро и надежно дождаться завершения без циклов update().
        """
        thread = self._current_worker
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        self.process_queue()
        return True

    def _cleanup(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._cancel_poll()

    def _on_close(self) -> None:
        self._cleanup()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self._cleanup()
