"""Тесты для модуля desktop/park_card_header.py."""

from __future__ import annotations

import os
import unittest
from typing import Callable

import tkinter as tk
from desktop.park_card_header import ParkCardHeader, _truncate_text


def _is_tk_available() -> bool:
    """Проверяет доступность графического сервера Tkinter (DISPLAY)."""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


TK_AVAILABLE = _is_tk_available()


class TestTruncateText(unittest.TestCase):
    """Тесты функции усечения текста с многоточием."""

    def setUp(self) -> None:
        if not TK_AVAILABLE:
            self.skipTest("DISPLAY не найден; пропуск Tkinter тестов")
        self.root = tk.Tk()
        import tkinter.font as tkfont
        self.font = tkfont.Font(family="Helvetica", size=8)

    def tearDown(self) -> None:
        if hasattr(self, "root") and self.root:
            self.root.destroy()

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
        if not TK_AVAILABLE:
            self.skipTest("DISPLAY не найден; пропуск Tkinter тестов")
        self.root = tk.Tk()
        self.root.deiconify()
        self.root.focus_force()
        self.root.update()
        self.toggle_count = 0

    def tearDown(self) -> None:
        if hasattr(self, "root") and self.root:
            self.root.destroy()

    def _on_toggle(self) -> None:
        self.toggle_count += 1

    def _create_header(
        self,
        parent: tk.Misc | None = None,
        on_toggle: Callable[[], None] | None = None,
    ) -> ParkCardHeader:
        p = parent if parent is not None else self.root
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
        self.root.update()

        # Клик мышью по чекбоксу
        self.assertEqual(self.toggle_count, 0)
        header.checkbox.event_generate("<Button-1>")
        self.root.update()

        # Callback вызван, но selected остался False (без подмены selected)
        self.assertEqual(self.toggle_count, 1)
        self.assertFalse(header.selected)

        # Нажатие пробела на чекбоксе
        header.checkbox.focus_set()
        header.checkbox.event_generate("<space>")
        self.root.update()

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
        self.root.update()
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
        self.root.update()

        # Клик и пробел не должны вызывать on_toggle
        header.checkbox.event_generate("<Button-1>")
        header.checkbox.event_generate("<space>")
        header.event_generate("<space>")
        self.root.update()

        self.assertEqual(self.toggle_count, 0)
        self.assertFalse(header.selected)
        self.assertEqual(int(header.checkbox.cget("takefocus")), 0)

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
        self.root.focus_force()
        self.root.update()

        self.assertEqual(int(header.checkbox.cget("takefocus")), 1)
        self.assertEqual(header.checkbox.cget("highlightcolor"), self.BORDER_COLOR)
        self.assertEqual(header.checkbox.cget("highlightbackground"), self.BG)

        # Проверяем делегирование focus_set
        header.focus_set()
        self.root.update()
        self.assertTrue(header.has_focus or self.root.focus_get() == header.checkbox)

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
        self.root.update()
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
        self.root.update()
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
        self.root.update()
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
        self.root.update()
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
        self.root.update()
        self.assertFalse(header.usb_badge.winfo_ismapped())
        self.assertFalse(header.hub_badge.winfo_ismapped())

    def test_badges_never_overlap_text_at_145_to_170_px(self) -> None:
        """Проверяет ширины 145, 154 и 170 px: бейджи не рисуются поверх текста."""
        for width in (145, 154, 170):
            with self.subTest(width=width):
                card = tk.Frame(self.root, width=width, height=120, bg=self.BG)
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
                    self.root.update()

                    # Проверяем отсутствие пересечения прямоугольников бейджей и статуса
                    b_frame = header.badges_frame
                    s_label = header.status_label

                    bx, by, bw, bh = b_frame.winfo_x(), b_frame.winfo_y(), b_frame.winfo_width(), b_frame.winfo_height()
                    sx, sy, sw, sh = s_label.winfo_x(), s_label.winfo_y(), s_label.winfo_width(), s_label.winfo_height()

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
        """При нехватке ширины (145 и 154 px) строка статуса переносится под бейджи."""
        for width in (145, 154):
            with self.subTest(width=width):
                card = tk.Frame(self.root, width=width, height=120, bg=self.BG)
                card.pack_propagate(False)
                card.pack()

                header = self._create_header(parent=card)
                header.pack(fill=tk.X)

                header.update_header(
                    selected=False,
                    status="Видео · USB",
                    status_color=self.USB_COLOR,
                    usb=True,
                    hub=True,
                    hub_online=True,
                )
                self.root.update()

                self.assertTrue(header.is_wrapped)
                self.assertGreater(header.status_label.winfo_y(), header.badges_frame.winfo_y())
                # Полный текст должен сохраниться без усечения
                self.assertEqual(header.status_label.cget("text"), "Видео · USB")

                card.destroy()

    def test_no_horizontal_card_expansion_with_long_status(self) -> None:
        """Проверяет, что длинный статус усекается многоточием без горизонтального роста карточки."""
        for width in (145, 154, 170):
            with self.subTest(width=width):
                card = tk.Frame(self.root, width=width, height=120, bg=self.BG)
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
                self.root.update()

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
