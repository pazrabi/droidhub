"""Автономный компонент выбора телефонов для сценариев диагностики DroidHub Desktop.

Компонент наследуется от ttk.Frame и предназначен для вставки в Toplevel-окно,
создаваемое хостом. Не выполняет запуск сценариев, не обращается к ADB напрямую
и не управляет модальностью родительского окна.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Iterable, Sequence

try:
    from desktop import theme
except ImportError:
    theme = None

logger = logging.getLogger(__name__)

EMPTY_MESSAGE = "Нет доступных телефонов для диагностики."
EMPTY_HINT = "Подключите устройства по USB и авторизуйте ADB-доступ."


def deduplicate_phones(phones: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Сохраняет порядок снимка и исключает повторные серийные номера.

    Каждое устройство представлено парой (exact_usb_serial, ready_label).
    При наличии дубликатов сохраняется первое вхождение.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for item in phones:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        serial = str(item[0])
        label = str(item[1])
        if serial not in seen:
            seen.add(serial)
            result.append((serial, label))
    return result


def resolve_selected_ids(
    phones: Sequence[tuple[str, str]],
    selected_ids: Iterable[str],
) -> list[str]:
    """Возвращает выбранные идентификаторы в порядке исходного снимка устройств.

    Идентификаторы, отсутствующие в списке phones, игнорируются.
    """
    valid = set(selected_ids)
    return [serial for serial, _ in phones if serial in valid]


class PhoneSelectionModel:
    """Изолированная логика выбора телефонов без привязки к графической подсистеме Tk."""

    def __init__(
        self,
        phones: Sequence[tuple[str, str]],
        selected_ids: Iterable[str] = (),
    ) -> None:
        self._phones = deduplicate_phones(phones)
        available_ids = {s for s, _ in self._phones}
        self._selected: set[str] = {str(s) for s in selected_ids if str(s) in available_ids}

    @property
    def phones(self) -> list[tuple[str, str]]:
        return list(self._phones)

    @property
    def is_empty(self) -> bool:
        return len(self._phones) == 0

    @property
    def all_selected(self) -> bool:
        return bool(self._phones) and len(self._selected) == len(self._phones)

    def is_selected(self, serial: str) -> bool:
        return serial in self._selected

    def select(self, serial: str) -> None:
        if serial in {s for s, _ in self._phones}:
            self._selected.add(serial)

    def deselect(self, serial: str) -> None:
        self._selected.discard(serial)

    def toggle(self, serial: str) -> bool:
        if serial in self._selected:
            self._selected.remove(serial)
            return False
        elif serial in {s for s, _ in self._phones}:
            self._selected.add(serial)
            return True
        return False

    def select_all(self) -> None:
        self._selected = {s for s, _ in self._phones}

    def deselect_all(self) -> None:
        self._selected.clear()

    def toggle_all(self) -> None:
        if self.all_selected:
            self.deselect_all()
        else:
            self.select_all()

    def set_selected_ids(self, selected_ids: Iterable[str]) -> None:
        available_ids = {s for s, _ in self._phones}
        self._selected = {str(s) for s in selected_ids if str(s) in available_ids}

    def get_selected_ids(self) -> list[str]:
        return [s for s, _ in self._phones if s in self._selected]


class DiagnosticPhonePicker(ttk.Frame):
    """Автономный фрейм выбора телефонов для сценариев диагностики DroidHub Desktop.

    Контракт:
        DiagnosticPhonePicker(
            parent,
            phones: Sequence[tuple[str, str]],
            *,
            selected_ids: Iterable[str] = (),
            on_apply: Callable[[list[str]], None],
            on_cancel: Callable[[], None],
        )
    """

    def __init__(
        self,
        parent: Any,
        phones: Sequence[tuple[str, str]],
        *,
        selected_ids: Iterable[str] = (),
        on_apply: Callable[[list[str]], None],
        on_cancel: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, **kwargs)

        self._on_apply = on_apply
        self._on_cancel = on_cancel
        self._model = PhoneSelectionModel(phones, selected_ids)
        self._phones = self._model.phones

        self._init_theme()
        self._create_widgets()
        self._setup_bindings()
        self._focus_initial()

    @property
    def phones(self) -> list[tuple[str, str]]:
        """Дедуплицированный список (serial, label) в порядке снимка."""
        return list(self._phones)

    @property
    def selected_ids(self) -> list[str]:
        """Текущие выбранные серийные номера устройств."""
        return self.get_selected_ids()

    def _init_theme(self) -> None:
        """Безопасное применение темы desktop/theme.py, если модуль доступен."""
        if theme is None:
            return

        for method_name in ("apply_theme", "setup_theme"):
            fn = getattr(theme, method_name, None)
            if callable(fn):
                try:
                    fn(self)
                    return
                except TypeError:
                    try:
                        fn()
                        return
                    except Exception:
                        pass
                except Exception:
                    pass

    def _create_widgets(self) -> None:
        """Создает визуальные элементы управления."""
        # 1. Верхний информационный блок или список
        self._content_frame = ttk.Frame(self)
        self._content_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 4))

        # Создаем пустую плашку для сообщения об отсутствии устройств
        self._empty_label = ttk.Label(
            self._content_frame,
            text=f"{EMPTY_MESSAGE}\n{EMPTY_HINT}",
            justify=tk.CENTER,
            padding=16,
        )
        self.empty_label = self._empty_label

        # Создаем Listbox для выбора устройств
        listbox_kwargs: dict[str, Any] = {
            "selectmode": tk.MULTIPLE,
            "exportselection": False,
            "takefocus": True,
        }

        # Применяем цвета из theme.py, если они определены
        if theme is not None:
            bg = getattr(theme, "RECESS", getattr(theme, "SURFACE", getattr(theme, "BG_PANEL", getattr(theme, "BG_SIDEBAR", getattr(theme, "BG_COLOR", None)))))
            fg = getattr(theme, "TEXT", getattr(theme, "FG_PANEL", getattr(theme, "FG_TEXT", getattr(theme, "FG_COLOR", None))))
            sel_bg = getattr(theme, "SELECTED", getattr(theme, "SELECT_BG", getattr(theme, "ACCENT_COLOR", None)))
            sel_fg = getattr(theme, "ACCENT", getattr(theme, "SELECT_FG", getattr(theme, "ACCENT_FG", None)))
            font = getattr(theme, "BODY_FONT", getattr(theme, "FONT_REGULAR", getattr(theme, "FONT", None)))

            if bg:
                listbox_kwargs["bg"] = bg
            if fg:
                listbox_kwargs["fg"] = fg
            if sel_bg:
                listbox_kwargs["selectbackground"] = sel_bg
            if sel_fg:
                listbox_kwargs["selectforeground"] = sel_fg
            if font:
                listbox_kwargs["font"] = font

        self._list_container = ttk.Frame(self._content_frame)
        self._scrollbar = ttk.Scrollbar(self._list_container, orient=tk.VERTICAL)
        listbox_kwargs["yscrollcommand"] = self._scrollbar.set

        self._listbox = tk.Listbox(self._list_container, **listbox_kwargs)
        self.listbox = self._listbox
        self._scrollbar.config(command=self._listbox.yview)

        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        if not self._phones:
            # Пустое состояние: скрываем список и показываем информационное сообщение
            self._list_container.pack_forget()
            self._empty_label.pack(fill=tk.BOTH, expand=True)
        else:
            self._empty_label.pack_forget()
            self._list_container.pack(fill=tk.BOTH, expand=True)

            # Заполняем список и проставляем существующий выбор
            for idx, (serial, label) in enumerate(self._phones):
                display_text = label if label else serial
                self._listbox.insert(tk.END, display_text)
                if self._model.is_selected(serial):
                    self._listbox.select_set(idx)

        # 2. Нижняя панель действий: «Все телефоны», «Применить», «Отмена»
        self._button_frame = ttk.Frame(self)
        self._button_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=6, pady=(4, 6))

        self._all_button = ttk.Button(
            self._button_frame,
            text="Все телефоны",
            command=self.select_all,
        )
        self._all_button.pack(side=tk.LEFT)

        self._cancel_button = ttk.Button(
            self._button_frame,
            text="Отмена",
            command=self.cancel,
        )
        self._cancel_button.pack(side=tk.RIGHT, padx=(6, 0))

        self._apply_button = ttk.Button(
            self._button_frame,
            text="Применить",
            command=self.apply,
        )
        self._apply_button.pack(side=tk.RIGHT)

        # Синонимы для кнопок для надежной интеграции и тестов
        self.all_button = self._all_button
        self.all_phones_button = self._all_button
        self.all_btn = self._all_button
        self.apply_button = self._apply_button
        self.apply_btn = self._apply_button
        self.cancel_button = self._cancel_button
        self.cancel_btn = self._cancel_button

    def _setup_bindings(self) -> None:
        """Настраивает горячие клавиши Enter и Escape."""
        # Enter и Escape на самом контейнере
        for seq in ("<Return>", "<KP_Enter>"):
            try:
                self.bind(seq, lambda _e: self.apply())
            except Exception:
                pass
        try:
            self.bind("<Escape>", lambda _e: self.cancel())
        except Exception:
            pass

        # На списке
        if hasattr(self, "_listbox") and self._listbox is not None:
            for seq in ("<Return>", "<KP_Enter>"):
                try:
                    self._listbox.bind(seq, lambda _e: (self.apply(), "break")[1])
                except Exception:
                    pass
            try:
                self._listbox.bind("<Escape>", lambda _e: (self.cancel(), "break")[1])
            except Exception:
                pass
            try:
                self._listbox.bind("<space>", self._on_space)
            except Exception:
                pass
            try:
                self._listbox.bind("<<ListboxSelect>>", self._sync_model_from_listbox)
            except Exception:
                pass

        # На кнопках
        for btn in (
            getattr(self, "_all_button", None),
            getattr(self, "_apply_button", None),
            getattr(self, "_cancel_button", None),
        ):
            if btn is not None:
                try:
                    btn.bind("<Escape>", lambda _e: (self.cancel(), "break")[1])
                except Exception:
                    pass

    def _sync_model_from_listbox(self, _event: tk.Event | None = None) -> None:
        """Синхронизирует внутреннюю модель выбора с выделением в Listbox."""
        if hasattr(self, "_listbox") and self._listbox is not None:
            try:
                if self._listbox.winfo_exists():
                    selected_indices = {int(i) for i in self._listbox.curselection()}
                    current_ids = [
                        self._phones[i][0]
                        for i in range(len(self._phones))
                        if i in selected_indices
                    ]
                    self._model.set_selected_ids(current_ids)
            except Exception:
                pass

    def _on_space(self, _event: tk.Event | None = None) -> str:
        """Переключает выбор элемента по клавише Пробел и синхронизирует модель."""
        try:
            active_idx = self._listbox.index(tk.ACTIVE)
            if active_idx is not None and 0 <= active_idx < len(self._phones):
                if self._listbox.selection_includes(active_idx):
                    self._listbox.selection_clear(active_idx)
                else:
                    self._listbox.selection_set(active_idx)
                self._sync_model_from_listbox()
                try:
                    self._listbox.event_generate("<<ListboxSelect>>")
                except Exception:
                    pass
        except Exception:
            pass
        return "break"

    def _focus_initial(self) -> None:
        """Устанавливает фокус на список (или кнопку Применить при пустом списке)."""
        def _do_focus():
            try:
                if self._phones and hasattr(self, "_listbox") and self._listbox.winfo_exists():
                    self._listbox.focus_set()
                    self._listbox.activate(0)
                elif hasattr(self, "_apply_button") and self._apply_button.winfo_exists():
                    self._apply_button.focus_set()
                elif hasattr(self, "_cancel_button") and self._cancel_button.winfo_exists():
                    self._cancel_button.focus_set()
            except Exception:
                pass

        _do_focus()
        try:
            self.after_idle(_do_focus)
        except Exception:
            pass

    def focus_initial(self) -> None:
        """Публичный метод установки начального фокуса."""
        self._focus_initial()

    def get_selected_ids(self) -> list[str]:
        """Возвращает точные USB serial выбранных телефонов по порядку снимка."""
        if hasattr(self, "_listbox") and self._listbox is not None:
            try:
                if self._listbox.winfo_exists():
                    selected_indices = {int(i) for i in self._listbox.curselection()}
                    return [
                        self._phones[i][0]
                        for i in range(len(self._phones))
                        if i in selected_indices
                    ]
            except Exception:
                pass
        return self._model.get_selected_ids()

    def select_all(self) -> None:
        """Действие «Все телефоны»: выбрать все доступные устройства."""
        self._model.select_all()
        if hasattr(self, "_listbox") and self._listbox is not None:
            try:
                if self._listbox.winfo_exists():
                    self._listbox.select_set(0, tk.END)
            except Exception:
                pass

    def all_phones(self) -> None:
        """Синоним для действия «Все телефоны»."""
        self.select_all()

    def deselect_all(self) -> None:
        """Снять выбор со всех устройств."""
        self._model.deselect_all()
        if hasattr(self, "_listbox") and self._listbox is not None:
            try:
                if self._listbox.winfo_exists():
                    self._listbox.selection_clear(0, tk.END)
            except Exception:
                pass

    def toggle_all(self) -> None:
        """Инвертирует выбор (если все выбраны — снимает, иначе выбирает все)."""
        current = self.get_selected_ids()
        if self._phones and len(current) == len(self._phones):
            self.deselect_all()
        else:
            self.select_all()

    def set_selected_ids(self, selected_ids: Iterable[str]) -> None:
        """Устанавливает выбор программно."""
        self._model.set_selected_ids(selected_ids)
        if hasattr(self, "_listbox") and self._listbox is not None:
            try:
                if self._listbox.winfo_exists():
                    self._listbox.selection_clear(0, tk.END)
                    selected_set = set(self._model.get_selected_ids())
                    for idx, (serial, _) in enumerate(self._phones):
                        if serial in selected_set:
                            self._listbox.select_set(idx)
            except Exception:
                pass

    def apply(self) -> None:
        """Действие «Применить»: передает выбранные ID в коллбек on_apply."""
        selected = self.get_selected_ids()
        self._on_apply(selected)

    def cancel(self) -> None:
        """Действие «Отмена»: вызывает коллбек on_cancel."""
        self._on_cancel()
