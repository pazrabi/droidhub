"""Автономный компонент каталога скриптов DroidHub Desktop (Tk/ttk).

Обеспечивает компактное отображение каталога сценариев с карточками,
расширяемым реестром, доступной клавиатурной навигацией и контрактом интеграции
для возврата состояния и фокуса при переключении между каталогом и панелями сценариев.
"""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Iterator, Sequence

AUTOPILOT_ID = "autopilot"
PHONE_DIAGNOSTICS_ID = "phone_diagnostics"


@dataclass(frozen=True)
class ScriptCard:
    """Описатель карточки сценария в каталоге."""

    id: str
    title: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("ID карточки должен быть непустой строкой")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Название карточки должно быть непустой строкой")
        if not isinstance(self.description, str):
            raise ValueError("Описание карточки должно быть строкой")


DEFAULT_CARDS: tuple[ScriptCard, ...] = (
    ScriptCard(
        id=AUTOPILOT_ID,
        title="Накрутка",
        description="Вкладка открывает имеющийся Автопилот",
    ),
    ScriptCard(
        id=PHONE_DIAGNOSTICS_ID,
        title="Диагностика телефона",
        description="Имеющиеся проверки готовности и USB",
    ),
)


def _normalize_card(item: ScriptCard | dict[str, Any] | tuple[str, str, str]) -> ScriptCard:
    """Приводит входной объект к экземпляру ScriptCard."""
    if isinstance(item, ScriptCard):
        return item
    if isinstance(item, dict):
        if "id" not in item or "title" not in item:
            raise ValueError("Словарь карточки должен содержать ключи 'id' и 'title'")
        return ScriptCard(
            id=str(item["id"]).strip(),
            title=str(item["title"]).strip(),
            description=str(item.get("description", "")),
        )
    if isinstance(item, (tuple, list)) and len(item) == 3:
        return ScriptCard(
            id=str(item[0]).strip(),
            title=str(item[1]).strip(),
            description=str(item[2]),
        )
    raise TypeError(f"Неподдерживаемый тип карточки: {type(item)!r}")


class ScriptCatalogRegistry:
    """Реестр карточек сценариев с проверкой уникальности ID."""

    def __init__(
        self,
        cards: Sequence[ScriptCard | dict[str, Any] | tuple[str, str, str]] | None = None,
        *,
        extra_cards: Sequence[ScriptCard | dict[str, Any] | tuple[str, str, str]] | None = None,
        extra_scripts: Sequence[ScriptCard | dict[str, Any] | tuple[str, str, str]] | None = None,
    ) -> None:
        self._cards: list[ScriptCard] = []
        base = cards if cards is not None else DEFAULT_CARDS
        for item in base:
            self.register(item)

        extras: list[Any] = []
        if extra_cards:
            extras.extend(extra_cards)
        if extra_scripts:
            extras.extend(extra_scripts)

        for item in extras:
            self.register(item)

    def register(self, card: ScriptCard | dict[str, Any] | tuple[str, str, str]) -> ScriptCard:
        """Регистрирует новую карточку сценария.

        Raises:
            ValueError: если карточка с таким ID уже зарегистрирована.
        """
        norm = _normalize_card(card)
        if self.has_id(norm.id):
            raise ValueError(f"Карточка сценария с ID '{norm.id}' уже зарегистрирована")
        self._cards.append(norm)
        return norm

    def has_id(self, card_id: str) -> bool:
        """Проверяет наличие карточки с данным ID в реестре."""
        return any(c.id == card_id for c in self._cards)

    def get(self, card_id: str) -> ScriptCard | None:
        """Возвращает карточку по ID либо None."""
        for c in self._cards:
            if c.id == card_id:
                return c
        return None

    def get_cards(self) -> tuple[ScriptCard, ...]:
        """Возвращает кортеж зарегистрированных карточек."""
        return tuple(self._cards)

    def __len__(self) -> int:
        return len(self._cards)

    def __iter__(self) -> Iterator[ScriptCard]:
        return iter(self._cards)

    def __getitem__(self, index: int) -> ScriptCard:
        return self._cards[index]


class ScriptCatalogView(ttk.Frame):
    """Автономный Tk/ttk виджет каталога скриптов DroidHub Desktop.

    Контракт интеграции:
        view = ScriptCatalogView(parent, on_open=callback, ...)
        view.show_catalog(card_id: str | None = None)
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_open: Callable[[str], None],
        extra_cards: Sequence[ScriptCard | dict[str, Any] | tuple[str, str, str]] | None = None,
        extra_scripts: Sequence[ScriptCard | dict[str, Any] | tuple[str, str, str]] | None = None,
        cards: Sequence[ScriptCard | dict[str, Any] | tuple[str, str, str]] | None = None,
        registry: ScriptCatalogRegistry | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, **kwargs)
        if not callable(on_open):
            raise TypeError("Параметр on_open должен быть вызываемым объектом (Callable[[str], None])")

        self._on_open = on_open
        if registry is not None:
            self._registry = registry
            extras: list[Any] = []
            if extra_cards:
                extras.extend(extra_cards)
            if extra_scripts:
                extras.extend(extra_scripts)
            for item in extras:
                self._registry.register(item)
        else:
            self._registry = ScriptCatalogRegistry(
                cards=cards,
                extra_cards=extra_cards,
                extra_scripts=extra_scripts,
            )

        self._last_opened_id: str | None = None
        self._focused_card_id: str | None = None
        self._card_buttons: dict[str, ttk.Button] = {}
        self._card_frames: dict[str, ttk.Widget] = {}
        self._card_order: list[str] = []

        self._setup_ui()

    @property
    def registry(self) -> ScriptCatalogRegistry:
        """Реестр карточек каталога."""
        return self._registry

    @property
    def cards(self) -> tuple[ScriptCard, ...]:
        """Кортеж карточек каталога."""
        return self._registry.get_cards()

    @property
    def last_opened_id(self) -> str | None:
        """ID последней карточки, вызвавшей on_open."""
        return self._last_opened_id

    @property
    def focused_card_id(self) -> str | None:
        """ID текущей сфокусированной или активной карточки."""
        return self._focused_card_id

    def get_button(self, card_id: str) -> ttk.Button | None:
        """Возвращает кнопку действия для указанной карточки."""
        return self._card_buttons.get(card_id)

    def get_card_frame(self, card_id: str) -> ttk.Widget | None:
        """Возвращает рамку указанной карточки."""
        return self._card_frames.get(card_id)

    def show_catalog(self, card_id: str | None = None) -> str | None:
        """Возвращает фокус и состояние к каталогу.

        Восстанавливает фокус на кнопке карточки, из которой была открыта панель
        (self._last_opened_id), либо на указанной в card_id, либо на первой карточке каталога.

        Returns:
            str | None: ID карточки, получившей фокус.
        """
        target_id = card_id or self._last_opened_id
        if not target_id and self._card_order:
            target_id = self._card_order[0]

        try:
            self.lift()
        except Exception:
            pass

        if target_id and target_id in self._card_buttons:
            btn = self._card_buttons[target_id]
            try:
                btn.focus_set()
            except Exception:
                pass
            self._focused_card_id = target_id
            return target_id
        return None

    def _setup_ui(self) -> None:
        """Формирует раскладку и виджеты каталога."""
        # Верхний заголовок блока каталога
        header_frame = ttk.Frame(self, padding=(16, 12, 16, 6))
        header_frame.pack(fill=tk.X, side=tk.TOP)

        title_lbl = ttk.Label(
            header_frame,
            text="Каталог сценариев",
            font=("Helvetica", 12, "bold"),
        )
        title_lbl.pack(anchor=tk.W)

        desc_lbl = ttk.Label(
            header_frame,
            text="Выберите сценарий для перехода к соответствующей панели управления:",
            font=("Helvetica", 9),
        )
        desc_lbl.pack(anchor=tk.W, pady=(2, 0))

        # Холст с вертикальной прокруткой для гарантированной адаптации на 940x540
        self._canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._cards_container = ttk.Frame(self._canvas, padding=(12, 6, 12, 12))
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._cards_container, anchor="nw"
        )

        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._cards_container.bind("<Configure>", self._on_container_configure)

        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Двухколоночная сетка для карточек
        self._cards_container.columnconfigure(0, weight=1, uniform="catalog_col")
        self._cards_container.columnconfigure(1, weight=1, uniform="catalog_col")

        # Привязка колеса мыши
        self.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.bind("<Button-4>", self._on_mousewheel, add="+")
        self.bind("<Button-5>", self._on_mousewheel, add="+")

        self._render_cards()

    def _render_cards(self) -> None:
        """Отрисовывает карточки сценариев из реестра."""
        cards = self._registry.get_cards()
        self._card_order = [c.id for c in cards]

        for idx, card in enumerate(cards):
            row, col = divmod(idx, 2)

            card_frame = ttk.LabelFrame(
                self._cards_container,
                text=f" {card.title} ",
                padding=(14, 10, 14, 10),
            )
            card_frame.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
            self._card_frames[card.id] = card_frame

            id_lbl = ttk.Label(
                card_frame,
                text=f"ID: {card.id}",
                font=("Helvetica", 8),
                foreground="#555555",
            )
            id_lbl.pack(anchor=tk.W, pady=(0, 4))

            desc_lbl = ttk.Label(
                card_frame,
                text=card.description,
                font=("Helvetica", 9),
                wraplength=380,
                justify=tk.LEFT,
            )
            desc_lbl.pack(anchor=tk.W, fill=tk.BOTH, expand=True, pady=(0, 8))

            btn_frame = ttk.Frame(card_frame)
            btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

            btn = ttk.Button(
                btn_frame,
                text="Открыть",
                command=lambda cid=card.id: self._trigger_open(cid),
            )
            btn.pack(side=tk.RIGHT)
            self._card_buttons[card.id] = btn

            labels = [id_lbl, desc_lbl]
            self._bind_card_navigation(card.id, idx, card_frame, btn, labels)

    def _bind_card_navigation(
        self,
        card_id: str,
        index: int,
        card_frame: ttk.Widget,
        btn: ttk.Button,
        labels: list[ttk.Widget],
    ) -> None:
        """Настраивает доступную клавиатурную навигацию и обработку кликов."""
        # Клавиши активации кнопки
        btn.bind("<Return>", lambda e, b=btn: (b.invoke(), "break")[1])
        btn.bind("<KP_Enter>", lambda e, b=btn: (b.invoke(), "break")[1])

        # Стрелочная навигация между карточками
        btn.bind("<Right>", lambda e, i=index: (self._focus_index(i + 1), "break")[1])
        btn.bind("<Down>", lambda e, i=index: (self._focus_index(i + 1), "break")[1])
        btn.bind("<Left>", lambda e, i=index: (self._focus_index(i - 1), "break")[1])
        btn.bind("<Up>", lambda e, i=index: (self._focus_index(i - 1), "break")[1])
        btn.bind("<Home>", lambda e: (self._focus_index(0), "break")[1])
        btn.bind("<End>", lambda e: (self._focus_index(len(self._card_order) - 1), "break")[1])

        # Отслеживание фокуса
        btn.bind("<FocusIn>", lambda e, cid=card_id: self._set_focused_id(cid))

        # Клик мыши по телу карточки переводит фокус и активирует сценарий
        card_frame.bind("<Button-1>", lambda e, b=btn: self._on_card_click(b))
        for lbl in labels:
            lbl.bind("<Button-1>", lambda e, b=btn: self._on_card_click(b))

    def _on_card_click(self, btn: ttk.Button) -> None:
        """Обрабатывает клик по карточке, фокусируя кнопку и запуская переход."""
        btn.focus_set()
        btn.invoke()

    def _set_focused_id(self, card_id: str) -> None:
        """Фиксирует текущий ID сфокусированной карточки."""
        self._focused_card_id = card_id

    def _focus_index(self, new_idx: int) -> None:
        """Устанавливает фокус на кнопку карточки по циклическому индексу."""
        if not self._card_order:
            return
        wrapped_idx = new_idx % len(self._card_order)
        target_id = self._card_order[wrapped_idx]
        btn = self._card_buttons.get(target_id)
        if btn:
            btn.focus_set()
            self._focused_card_id = target_id

    def _trigger_open(self, card_id: str) -> None:
        """Единая точка активации карточки: запоминает состояние и вызывает on_open."""
        self._last_opened_id = card_id
        self._focused_card_id = card_id
        self._on_open(card_id)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        """Синхронизирует ширину внутреннего контейнера с холстом."""
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        self._update_scrollbar()

    def _on_container_configure(self, event: tk.Event) -> None:
        """Обновляет область прокрутки при изменении размеров карточек."""
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        """Показывает полосу прокрутки только при необходимости."""
        try:
            if not self.winfo_exists() or not self._canvas.winfo_exists():
                return
            bbox = self._canvas.bbox("all")
            if not bbox:
                return
            content_height = bbox[3] - bbox[1]
            canvas_height = self._canvas.winfo_height()
            if content_height > canvas_height > 1:
                if not self._scrollbar.winfo_ismapped():
                    self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            else:
                if self._scrollbar.winfo_ismapped():
                    self._scrollbar.pack_forget()
        except Exception:
            pass

    def _on_mousewheel(self, event: tk.Event) -> None:
        """Обрабатывает прокрутку колесом мыши."""
        try:
            if not self.winfo_exists() or not self._canvas.winfo_exists():
                return
            if event.num == 4:
                self._canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self._canvas.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass
