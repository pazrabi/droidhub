"""Тесты для модуля desktop/park_card_header.py."""

from __future__ import annotations

import unittest
from typing import Callable

import tkinter as tk
import tkinter.font as tkfont
from desktop.park_card_header import ParkCardHeader, _truncate_text

# ---------------------------------------------------------------------------
# Single root Tk created once for the entire module to avoid Tk 9 / Aqua
# segfault that occurs when a probe Tk() is created and destroyed at import
# time before the real root is constructed.  On headless servers TclError is
# caught and all Tk-dependent tests are skipped.
# ---------------------------------------------------------------------------

_root: tk.Tk | None = None
_TK_SKIP_REASON: str | None = None


def setUpModule() -> None:  # noqa: N802
    global _root, _TK_SKIP_REASON
    try:
        _root = tk.Tk()
        _root.withdraw()
    except tk.TclError as exc:
        _TK_SKIP_REASON = f"Tkinter недоступен: {exc}"


def tearDownModule() -> None:  # noqa: N802
    global _root
    if _root is not None:
        try:
            _root.destroy()
        except Exception:
            pass
        _root = None


def _skip_if_no_tk(test_case: unittest.TestCase) -> None:
    if _TK_SKIP_REASON is not None:
        test_case.skipTest(_TK_SKIP_REASON)


class TestTruncateText(unittest.TestCase):
    """Тесты функции усечения текста с многоточием."""

    def setUp(self) -> None:
        _skip_if_no_tk(self)
        assert _root is not None
        self.font = tkfont.Font(root=_root, family="Helvetica", size=8)

    def tearDown(self) -> None:
        # Font objects are cleaned up when root is destroyed; no explicit delete needed.
        pass

    def test_truncate_empty(self) -> None:
        self.assertEqual(_truncate_text("", self.font, 100), "")
        self.assertEqual(_truncate_text("Текст", self.font, 0), "")

    def test_truncate_fits_no_ellipsis(self) -> None:
        text = "Готов"
        res = _truncate_text(text, self.font, 200)
        self.assertEqual(res, text)

    def test_truncate_exceeds_appends_ellipsis(self) -> None:
        text = "Очень длинная строка для проверки усечения многоточием"
        max_w = 80
        res = _truncate_text(text, self.font, max_w)
        self.assertTrue(res.endswith("…"))
        self.assertLessEqual(self.font.measure(res), max_w)


class TestParkCardHeader(unittest.TestCase):
    """Набор тестов для компонента ParkCardHeader."""

    BG = "#252526"
    TEXT_COLOR = "#ffffff"
    MUTED_COLOR = "#888888"
    USB_COLOR = "#4ec9b0"
    HUB_COLOR = "#dcdcaa"
    BORDER_COLOR = "#3c3c3c"

    def setUp(self) -> None:
        _skip_if_no_tk(self)
        assert _root is not None
        # Each test gets its own Toplevel so widgets are isolated and
        # destroyed in tearDown without touching the shared root interpreter.
        self._top = tk.Toplevel(_root)
        # deiconify + focus_force so keyboard events work (required on X11/xvfb)
        self._top.deiconify()
        self._top.focus_force()
        _root.update()
        self.toggle_count = 0

    def tearDown(self) -> None:
        try:
            self._top.destroy()
        except Exception:
            pass

    def _on_toggle(self) -> None:
        self.toggle_count += 1

    def _create_header(
        self,
        parent: tk.Misc | None = None,
        on_toggle: Callable[[], None] | None = None,
    ) -> ParkCardHeader:
        p = parent if parent is not None else self._top
        cb = on_toggle if on_toggle is not None else self._on_toggle
        return ParkCardHeader(
            p,
            on_toggle=cb,
            bg=self.BG,
            text_color=self.TEXT_COLOR,
            muted_color=self.MUTED_COLOR,
            usb_color=self.USB_COLOR,
            hub_color=self.HUB_COLOR,
            border_color=self.BORDER_COLOR,
        )

    def _pump(self) -> None:
        """Process pending Tk events including after_idle callbacks."""
        assert _root is not None
        _root.update()

    def test_contract_and_initial_state(self) -> None:
        header = self._create_header()
        self.assertIsInstance(header, tk.Frame)
        self.assertFalse(header.selected)
        self.assertTrue(header.selectable)
        self.assertEqual(header.status, "")
        self.assertFalse(header.usb)
        self.assertFalse(header.hub)
        self.assertFalse(header.hub_online)

        self.assertIsInstance(header.checkbox, tk.Checkbutton)
        self.assertIsInstance(header.usb_badge, tk.Label)
        self.assertIsInstance(header.hub_badge, tk.Label)
        self.assertIsInstance(header.status_label, tk.Label)

    def test_update_header_state(self) -> None:
        header = self._create_header()
        header.update_header(
            selected=True,
            status="Видео · USB",
            status_color="#4ec9b0",
            usb=True,
            hub=True,
            hub_online=True,
            selectable=True,
        )
        self.assertTrue(header.selected)
        self.assertEqual(header.status, "Видео · USB")
        self.assertEqual(header.status_color, "#4ec9b0")
        self.assertTrue(header.usb)
        self.assertTrue(header.hub)
        self.assertTrue(header.hub_online)
        self.assertTrue(header.selectable)

    def test_toggle_callback_without_mutating_selected(self) -> None:
        header = self._create_header()
        header.pack()
        header.update_header(
            selected=False,
            status="Готов",
            status_color="#4ec9b0",
            usb=True,
            hub=False,
            selectable=True,
        )
        self._pump()

        # Клик мышью по чекбоксу
        self.assertEqual(self.toggle_count, 0)
        header.checkbox.event_generate("<Button-1>")
        self._pump()

        # Callback вызван, но selected остался False (без подмены selected)
        self.assertEqual(self.toggle_count, 1)
        self.assertFalse(header.selected)

        # Нажатие пробела на чекбоксе (требуется фокус для X11)
        header.checkbox.focus_set()
        self._pump()
        header.checkbox.event_generate("<space>")
        self._pump()

        self.assertEqual(self.toggle_count, 2)
        self.assertFalse(header.selected)

        # Хост явно обновляет selected=True
        header.update_header(
            selected=True,
            status="Готов",
            status_color="#4ec9b0",
            usb=True,
            hub=False,
            selectable=True,
        )
        self.assertTrue(header.selected)

        # Следующий клик: selected не сбрасывается в False сам по себе
        header.checkbox.event_generate("<Button-1>")
        self._pump()
        self.assertEqual(self.toggle_count, 3)
        self.assertTrue(header.selected)

    def test_selectable_false_blocks_interaction(self) -> None:
        header = self._create_header()
        header.pack()
        header.update_header(
            selected=False,
            status="Отключено",
            status_color="#888888",
            usb=False,
            hub=False,
            selectable=False,
        )
        self._pump()

        # Клик и пробел не должны вызывать on_toggle
        header.checkbox.event_generate("<Button-1>")
        header.checkbox.event_generate("<space>")
        header.event_generate("<space>")
        self._pump()

        self.assertEqual(self.toggle_count, 0)
        self.assertFalse(header.selected)
        self.assertEqual(int(header.checkbox.cget("takefocus")), 0)
        # selectable=False должен выставлять DISABLED на чекбокс
        self.assertEqual(str(header.checkbox.cget("state")), str(tk.DISABLED))

    def test_selectable_true_enables_checkbox(self) -> None:
        """После перехода selectable=False → True чекбокс переходит в NORMAL."""
        header = self._create_header()
        header.update_header(
            selected=False,
            status="Готов",
            status_color=self.USB_COLOR,
            usb=True,
            hub=False,
            selectable=False,
        )
        self.assertEqual(str(header.checkbox.cget("state")), str(tk.DISABLED))

        header.update_header(
            selected=False,
            status="Готов",
            status_color=self.USB_COLOR,
            usb=True,
            hub=False,
            selectable=True,
        )
        self.assertEqual(str(header.checkbox.cget("state")), str(tk.NORMAL))

    def test_clean_keyboard_focus(self) -> None:
        header = self._create_header()
        header.pack()
        header.update_header(
            selected=False,
            status="Готов",
            status_color="#4ec9b0",
            usb=True,
            hub=False,
            selectable=True,
        )
        self._pump()

        self.assertEqual(int(header.checkbox.cget("takefocus")), 1)
        self.assertEqual(header.checkbox.cget("highlightcolor"), self.BORDER_COLOR)
        self.assertEqual(header.checkbox.cget("highlightbackground"), self.BG)

        # Проверяем делегирование focus_set
        header.focus_set()
        self._pump()
        focused = self._top.focus_get() or _root.focus_get()
        self.assertTrue(header.has_focus or focused == header.checkbox)

    def test_route_badges_states_and_colors(self) -> None:
        header = self._create_header()
        header.pack()

        # 1. Только USB
        header.update_header(
            selected=False,
            status="Готов",
            status_color=self.USB_COLOR,
            usb=True,
            hub=False,
        )
        self._pump()
        self.assertTrue(header.usb_badge.winfo_ismapped())
        self.assertFalse(header.hub_badge.winfo_ismapped())
        self.assertEqual(header.usb_badge.cget("fg"), self.USB_COLOR)

        # 2. Только Hub, офлайн (hub_online=False)
        header.update_header(
            selected=False,
            status="Hub · Офлайн",
            status_color=self.MUTED_COLOR,
            usb=False,
            hub=True,
            hub_online=False,
        )
        self._pump()
        self.assertFalse(header.usb_badge.winfo_ismapped())
        self.assertTrue(header.hub_badge.winfo_ismapped())
        self.assertEqual(header.hub_badge.cget("fg"), self.MUTED_COLOR)

        # 3. Только Hub, онлайн (hub_online=True)
        header.update_header(
            selected=False,
            status="Hub · Онлайн",
            status_color=self.HUB_COLOR,
            usb=False,
            hub=True,
            hub_online=True,
        )
        self._pump()
        self.assertFalse(header.usb_badge.winfo_ismapped())
        self.assertTrue(header.hub_badge.winfo_ismapped())
        self.assertEqual(header.hub_badge.cget("fg"), self.HUB_COLOR)

        # 4. Обе стороны: USB + Hub (онлайн)
        header.update_header(
            selected=False,
            status="Видео · USB",
            status_color=self.USB_COLOR,
            usb=True,
            hub=True,
            hub_online=True,
        )
        self._pump()
        self.assertTrue(header.usb_badge.winfo_ismapped())
        self.assertTrue(header.hub_badge.winfo_ismapped())
        self.assertEqual(header.usb_badge.cget("fg"), self.USB_COLOR)
        self.assertEqual(header.hub_badge.cget("fg"), self.HUB_COLOR)

        # 5. Ни одной стороны
        header.update_header(
            selected=False,
            status="Нет связи",
            status_color=self.MUTED_COLOR,
            usb=False,
            hub=False,
        )
        self._pump()
        self.assertFalse(header.usb_badge.winfo_ismapped())
        self.assertFalse(header.hub_badge.winfo_ismapped())

    def test_route_change_before_first_show_no_hidden_badge(self) -> None:
        """Смена маршрута до первого показа не оставляет скрытый бейдж из-за winfo_ismapped."""
        # Create header but don't pack/show yet
        header = self._create_header()
        # Устанавливаем USB=True, потом USB=False — до show
        header.update_header(
            selected=False,
            status="Готов",
            status_color=self.USB_COLOR,
            usb=True,
            hub=False,
        )
        header.update_header(
            selected=False,
            status="Нет маршрута",
            status_color=self.MUTED_COLOR,
            usb=False,
            hub=False,
        )
        # Теперь показываем
        header.pack()
        self._pump()

        # USB бейдж должен быть скрыт (не смапирован)
        self.assertFalse(header.usb_badge.winfo_ismapped())
        self.assertFalse(header.hub_badge.winfo_ismapped())

    def test_badges_never_overlap_text_at_145_to_170_px(self) -> None:
        """Проверяет ширины 145, 154 и 170 px: бейджи не рисуются поверх текста."""
        for width in (145, 154, 170):
            with self.subTest(width=width):
                card = tk.Frame(self._top, width=width, height=120, bg=self.BG)
                card.pack_propagate(False)
                card.pack()

                header = self._create_header(parent=card)
                header.pack(fill=tk.X)

                for status_text in ("Видео · USB", "Hub · Онлайн"):
                    header.update_header(
                        selected=False,
                        status=status_text,
                        status_color=self.USB_COLOR,
                        usb=True,
                        hub=True,
                        hub_online=True,
                    )
                    self._pump()

                    # Проверяем отсутствие пересечения прямоугольников бейджей и статуса
                    b_frame = header.badges_frame
                    s_label = header.status_label

                    bx = b_frame.winfo_x()
                    by = b_frame.winfo_y()
                    bw = b_frame.winfo_width()
                    bh = b_frame.winfo_height()
                    sx = s_label.winfo_x()
                    sy = s_label.winfo_y()
                    sw = s_label.winfo_width()
                    sh = s_label.winfo_height()

                    # Если они на одной строке (y перекрывается), то по горизонтали x не должны пересекаться
                    y_overlap = not (sy >= by + bh or by >= sy + sh)
                    if y_overlap:
                        x_overlap = not (sx + sw <= bx or bx + bw <= sx)
                        self.assertFalse(
                            x_overlap,
                            f"Бейджи и текст пересеклись при ширине {width}px для '{status_text}'!",
                        )
                    else:
                        # Если строка перенесена, статус должен быть строго под бейджами (sy >= by + bh)
                        self.assertGreaterEqual(sy, by + bh)

                card.destroy()

    def test_status_wraps_under_badges_when_narrow(self) -> None:
        """Строка статуса переносится под бейджи когда текст гарантированно шире доступного места.

        Тест вычисляет row0_available из реальной геометрии виджетов (шрифт-независимо),
        затем строит статусный текст, чей измеренный размер превышает row0_available.
        Так тест корректен и на Linux/Xvfb и на macOS Aqua с любым системным шрифтом.
        """
        assert _root is not None
        font = tkfont.Font(root=_root, family="Helvetica", size=8)

        for width in (145, 154):
            with self.subTest(width=width):
                card = tk.Frame(self._top, width=width, height=120, bg=self.BG)
                card.pack_propagate(False)
                card.pack()

                header = self._create_header(parent=card)
                header.pack(fill=tk.X)
                self._pump()

                # Подготовим виджеты с обоими бейджами чтобы получить реальные размеры
                header.update_header(
                    selected=False,
                    status="x",
                    status_color=self.USB_COLOR,
                    usb=True,
                    hub=True,
                    hub_online=True,
                )
                self._pump()

                # Вычисляем row0_available из реальных reqwidth виджетов
                cb_w = header.checkbox.winfo_reqwidth()
                usb_w = header.usb_badge.winfo_reqwidth()
                hub_w = header.hub_badge.winfo_reqwidth()
                badges_w = usb_w + 2 + hub_w + 2  # pack padx=(2,0) each
                pad_row0 = 12
                row0_available = width - cb_w - badges_w - pad_row0

                # Строим текст, гарантированно шире row0_available по реальным измерениям шрифта
                base = "Статус"
                while font.measure(base) <= row0_available:
                    base += " USB"
                long_status = base

                # Убеждаемся что текст действительно шире (иначе тест бессмысленен)
                self.assertGreater(
                    font.measure(long_status),
                    row0_available,
                    f"Не удалось построить текст шире row0_available={row0_available}px",
                )
                # Текст должен влезть в полную ширину строки (row1_available = w - 4)
                # чтобы display_text == long_status (без усечения)
                row1_available = width - 4
                self.assertLessEqual(
                    font.measure(long_status),
                    row1_available,
                    f"Статус шире row1_available={row1_available}px — будет усечён многоточием, "
                    "проверьте логику теста",
                )

                header.update_header(
                    selected=False,
                    status=long_status,
                    status_color=self.USB_COLOR,
                    usb=True,
                    hub=True,
                    hub_online=True,
                )
                self._pump()

                self.assertTrue(
                    header.is_wrapped,
                    f"Ожидался перенос при width={width}px, текст={repr(long_status)}",
                )
                self.assertGreater(
                    header.status_label.winfo_y(),
                    header.badges_frame.winfo_y(),
                    "Строка статуса должна быть ниже бейджей",
                )
                # Полный текст должен сохраниться без усечения
                self.assertEqual(header.status_label.cget("text"), long_status)

                card.destroy()

    def test_no_horizontal_card_expansion_with_long_status(self) -> None:
        """Проверяет, что длинный статус усекается многоточием без горизонтального роста карточки."""
        for width in (145, 154, 170):
            with self.subTest(width=width):
                card = tk.Frame(self._top, width=width, height=120, bg=self.BG)
                card.pack_propagate(False)
                card.pack()

                header = self._create_header(parent=card)
                header.pack(fill=tk.X)

                long_status = "Очень длинный статус устройства онлайн " * 8
                header.update_header(
                    selected=False,
                    status=long_status,
                    status_color=self.USB_COLOR,
                    usb=True,
                    hub=True,
                    hub_online=True,
                )
                self._pump()

                # Ширина карточки не должна вырасти
                self.assertEqual(card.winfo_width(), width)
                # Текст статуса должен содержать многоточие
                displayed = header.status_label.cget("text")
                self.assertTrue(displayed.endswith("…"))
                # Запрошенная ширина заголовка не должна раздувать карточку
                self.assertLessEqual(header.status_label.winfo_width(), width)

                card.destroy()


if __name__ == "__main__":
    unittest.main()
