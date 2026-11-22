"""Автономный компонент заголовка карточки устройства в Парке DroidHub."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable


def _truncate_text(text: str, font: tkfont.Font, max_width: int, ellipsis: str = "…") -> str:
    """Ограничивает текст многоточием так, чтобы измеренная ширина не превышала max_width."""
    if not text or max_width <= 0:
        return ""
    if font.measure(text) <= max_width:
        return text

    ell_w = font.measure(ellipsis)
    if ell_w > max_width:
        return ""

    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if font.measure(candidate) <= max_width:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1

    return best if best else ellipsis


class ParkCardHeader(tk.Frame):
    """Заголовок карточки устройства для Парка DroidHub.

    Отображает:
    - Чекбокс выбора устройства с чистой фокусировкой клавиатурой.
    - Независимые бейджи маршрутов USB и Hub.
    - Краткий статус устройства от хоста.

    Бейджи и текст статуса никогда не перекрывают друг друга. При нехватке ширины
    (карточки 145-170 px) строка статуса переносится под бейджи или усекается
    многоточием без горизонтального раздувания карточки.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_toggle: Callable[[], None],
        bg: str,
        text_color: str,
        muted_color: str,
        usb_color: str,
        hub_color: str,
        border_color: str,
    ) -> None:
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0)

        self._on_toggle = on_toggle
        self._bg = bg
        self._text_color = text_color
        self._muted_color = muted_color
        self._usb_color = usb_color
        self._hub_color = hub_color
        self._border_color = border_color

        self._selected: bool = False
        self._status: str = ""
        self._status_color: str = text_color
        self._usb: bool = False
        self._hub: bool = False
        self._hub_online: bool = False
        self._selectable: bool = True

        # Tracks which badges are currently packed to avoid winfo_ismapped
        # issues before first display (winfo_ismapped is False until actually
        # drawn, so we track state independently).
        self._usb_badge_packed: bool = False
        self._hub_badge_packed: bool = False

        self._last_width: int = 0
        self._is_wrapped: bool = False
        self._has_focus: bool = False

        # Pending after_idle relayout id (to avoid nested update calls)
        self._relayout_after_id: str | None = None

        self._font = tkfont.Font(family="Helvetica", size=8)
        self._badge_font = tkfont.Font(family="Helvetica", size=7, weight="bold")

        self._create_widgets()
        self._bind_events()

    @property
    def selected(self) -> bool:
        return self._selected

    @property
    def selectable(self) -> bool:
        return self._selectable

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_color(self) -> str:
        return self._status_color

    @property
    def usb(self) -> bool:
        return self._usb

    @property
    def hub(self) -> bool:
        return self._hub

    @property
    def hub_online(self) -> bool:
        return self._hub_online

    @property
    def is_wrapped(self) -> bool:
        return self._is_wrapped

    @property
    def has_focus(self) -> bool:
        return self._has_focus

    @property
    def checkbox(self) -> tk.Checkbutton:
        return self._checkbox

    @property
    def usb_badge(self) -> tk.Label:
        return self._usb_badge

    @property
    def hub_badge(self) -> tk.Label:
        return self._hub_badge

    @property
    def badges_frame(self) -> tk.Frame:
        return self._badges_frame

    @property
    def status_label(self) -> tk.Label:
        return self._status_label

    def _create_widgets(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0)

        self._var = tk.BooleanVar(master=self, value=False)
        self._checkbox = tk.Checkbutton(
            self,
            variable=self._var,
            onvalue=True,
            offvalue=False,
            bg=self._bg,
            activebackground=self._bg,
            selectcolor=self._bg,
            fg=self._text_color,
            activeforeground=self._text_color,
            highlightthickness=1,
            highlightbackground=self._bg,
            highlightcolor=self._border_color,
            bd=0,
            padx=0,
            pady=0,
            takefocus=1,
            cursor="hand2",
        )
        self._checkbox.grid(row=0, column=0, sticky="w", padx=(0, 2))

        self._badges_frame = tk.Frame(self, bg=self._bg, bd=0, highlightthickness=0)

        self._usb_badge = tk.Label(
            self._badges_frame,
            text="USB",
            font=self._badge_font,
            fg=self._muted_color,
            bg=self._bg,
            padx=2,
            pady=0,
            highlightthickness=1,
            highlightbackground=self._border_color,
            bd=0,
        )

        self._hub_badge = tk.Label(
            self._badges_frame,
            text="Hub",
            font=self._badge_font,
            fg=self._muted_color,
            bg=self._bg,
            padx=2,
            pady=0,
            highlightthickness=1,
            highlightbackground=self._border_color,
            bd=0,
        )

        self._status_label = tk.Label(
            self,
            text="",
            font=self._font,
            fg=self._text_color,
            bg=self._bg,
            anchor="e",
            bd=0,
            highlightthickness=0,
        )
        self._status_label.grid(row=0, column=1, sticky="e", padx=(2, 2))

    def _bind_events(self) -> None:
        self._checkbox.bind("<Button-1>", self._on_checkbox_click)
        self._checkbox.bind("<space>", self._on_checkbox_space)
        self._checkbox.bind("<KP_Space>", self._on_checkbox_space)
        self._checkbox.bind("<FocusIn>", self._on_focus_in)
        self._checkbox.bind("<FocusOut>", self._on_focus_out)

        self.bind("<Configure>", self._on_configure)
        self.bind("<space>", self._on_checkbox_space)
        self.bind("<KP_Space>", self._on_checkbox_space)

    def _on_focus_in(self, event: tk.Event) -> None:
        self._has_focus = True

    def _on_focus_out(self, event: tk.Event) -> None:
        self._has_focus = False

    def _handle_toggle(self) -> None:
        if self._selectable:
            self._on_toggle()

    def _on_checkbox_click(self, event: tk.Event | None = None) -> str:
        if self._selectable:
            self._checkbox.focus_set()
            self._handle_toggle()
        return "break"

    def _on_checkbox_space(self, event: tk.Event | None = None) -> str:
        if self._selectable:
            self._handle_toggle()
        return "break"

    def focus_set(self) -> None:
        """Переводит фокус клавиатуры на чекбокс, если он доступен для выбора."""
        if self._selectable:
            self._checkbox.focus_set()
        else:
            super().focus_set()

    def update_header(
        self,
        *,
        selected: bool,
        status: str,
        status_color: str,
        usb: bool,
        hub: bool,
        hub_online: bool = False,
        selectable: bool = True,
    ) -> None:
        """Обновляет состояние заголовка карточки.

        Не подменяет selected локально: отображает только явно переданное значение.
        """
        self._selected = bool(selected)
        self._status = str(status)
        self._status_color = str(status_color)
        self._usb = bool(usb)
        self._hub = bool(hub)
        self._hub_online = bool(hub_online)
        self._selectable = bool(selectable)

        self._var.set(self._selected)
        if self._selectable:
            self._checkbox.config(
                state=tk.NORMAL,
                takefocus=1,
                cursor="hand2",
            )
        else:
            self._checkbox.config(
                state=tk.DISABLED,
                takefocus=0,
                cursor="",
            )

        # Use internal tracking (_usb_badge_packed / _hub_badge_packed) instead of
        # winfo_ismapped() so route changes before the first window draw are correct.
        if self._usb:
            self._usb_badge.config(fg=self._usb_color, text="USB")
            if not self._usb_badge_packed:
                self._usb_badge.pack(side=tk.LEFT, padx=(2, 0))
                self._usb_badge_packed = True
        else:
            self._usb_badge.config(fg=self._muted_color)
            if self._usb_badge_packed:
                self._usb_badge.pack_forget()
                self._usb_badge_packed = False

        if self._hub:
            h_fg = self._hub_color if self._hub_online else self._muted_color
            self._hub_badge.config(fg=h_fg, text="Hub")
            if not self._hub_badge_packed:
                self._hub_badge.pack(side=tk.LEFT, padx=(2, 0))
                self._hub_badge_packed = True
        else:
            self._hub_badge.config(fg=self._muted_color)
            if self._hub_badge_packed:
                self._hub_badge.pack_forget()
                self._hub_badge_packed = False

        if self._usb or self._hub:
            self._badges_frame.grid(row=0, column=2, sticky="e")
        else:
            self._badges_frame.grid_remove()

        self._relayout()

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if event.width <= 1:
            return
        # Relayout whenever the width changes OR equals the fallback (154) so
        # the first Configure event (which may report exactly 154) always
        # triggers a proper geometry pass.
        if event.width == self._last_width:
            return
        # Avoid calling update/update_idletasks from within Configure; schedule
        # relayout via after_idle so it runs outside the event handler.
        new_width = event.width
        if self._relayout_after_id is not None:
            try:
                self.after_cancel(self._relayout_after_id)
            except Exception:
                pass
            self._relayout_after_id = None
        self._relayout_after_id = self.after_idle(
            lambda w=new_width: self._relayout_from_idle(w)
        )

    def _relayout_from_idle(self, target_width: int) -> None:
        """Called from after_idle; performs relayout for the given width."""
        self._relayout_after_id = None
        self._relayout(target_width=target_width)

    def _relayout(self, target_width: int | None = None) -> None:
        if target_width is not None and target_width > 1:
            w = target_width
        elif self.winfo_width() > 1:
            w = self.winfo_width()
        elif hasattr(self.master, "winfo_width") and self.master.winfo_width() > 1:
            w = self.master.winfo_width()
        elif getattr(self.master, "cget", None) and int(self.master.cget("width") or 0) > 1:
            w = int(self.master.cget("width"))
        else:
            w = 154

        self._last_width = w

        cb_w = self._checkbox.winfo_reqwidth()
        badges_w = 0
        if self._usb:
            badges_w += self._usb_badge.winfo_reqwidth() + 2
        if self._hub:
            badges_w += self._hub_badge.winfo_reqwidth() + 2

        pad_row0 = 12
        row0_available = w - cb_w - badges_w - pad_row0
        status_w = self._font.measure(self._status)

        should_wrap = bool(badges_w > 0 and status_w > row0_available)

        if should_wrap:
            row1_available = max(10, w - 4)
            display_text = _truncate_text(self._status, self._font, row1_available)
            self._status_label.config(text=display_text, fg=self._status_color)
            self._status_label.grid_forget()
            self._status_label.grid(row=1, column=0, columnspan=3, sticky="e", padx=(0, 2), pady=(1, 0))
            self._is_wrapped = True
        else:
            avail = max(10, row0_available)
            display_text = _truncate_text(self._status, self._font, avail)
            self._status_label.config(text=display_text, fg=self._status_color)
            self._status_label.grid_forget()
            self._status_label.grid(row=0, column=1, sticky="e", padx=(2, 4 if badges_w > 0 else 2))
            self._is_wrapped = False
