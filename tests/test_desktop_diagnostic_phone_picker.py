"""Тесты для автономного компонента DiagnosticPhonePicker и чистой логики выбора."""

from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch

from desktop.diagnostic_phone_picker import (
    EMPTY_HINT,
    EMPTY_MESSAGE,
    DiagnosticPhonePicker,
    PhoneSelectionModel,
    deduplicate_phones,
    resolve_selected_ids,
)


def _check_tk_display_available() -> bool:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


HAS_DISPLAY = _check_tk_display_available()


class TestPhoneSelectionPureLogic(unittest.TestCase):
    """Чистые тесты логики выбора и обработки дубликатов (выполняются даже headless)."""

    def test_deduplicate_phones_preserves_snapshot_order(self) -> None:
        raw_phones = [
            ("serial_1", "Google Pixel 7"),
            ("serial_2", "Samsung Galaxy S23"),
            ("serial_3", "Xiaomi 13"),
        ]
        deduped = deduplicate_phones(raw_phones)
        self.assertEqual(
            deduped,
            [
                ("serial_1", "Google Pixel 7"),
                ("serial_2", "Samsung Galaxy S23"),
                ("serial_3", "Xiaomi 13"),
            ],
        )

    def test_deduplicate_phones_skips_repeated_ids_and_keeps_first(self) -> None:
        raw_phones = [
            ("serial_1", "Label First"),
            ("serial_2", "Label Two"),
            ("serial_1", "Label Duplicate 1"),
            ("serial_3", "Label Three"),
            ("serial_2", "Label Duplicate 2"),
            ("serial_1", "Label Duplicate 1 again"),
        ]
        deduped = deduplicate_phones(raw_phones)
        self.assertEqual(len(deduped), 3)
        self.assertEqual(
            deduped,
            [
                ("serial_1", "Label First"),
                ("serial_2", "Label Two"),
                ("serial_3", "Label Three"),
            ],
        )

    def test_deduplicate_phones_empty(self) -> None:
        self.assertEqual(deduplicate_phones([]), [])

    def test_resolve_selected_ids_preserves_snapshot_order(self) -> None:
        phones = [
            ("id_a", "Phone A"),
            ("id_b", "Phone B"),
            ("id_c", "Phone C"),
            ("id_d", "Phone D"),
        ]
        # Передаем выбор в обратном порядке
        selected_ids = ["id_c", "id_a"]
        resolved = resolve_selected_ids(phones, selected_ids)
        # Должен сохраниться порядок из phones: id_a, затем id_c
        self.assertEqual(resolved, ["id_a", "id_c"])

    def test_resolve_selected_ids_filters_unknown_and_duplicates(self) -> None:
        phones = [
            ("id_1", "Phone 1"),
            ("id_2", "Phone 2"),
        ]
        selected_ids = ["id_2", "id_unknown", "id_2", "id_other"]
        resolved = resolve_selected_ids(phones, selected_ids)
        self.assertEqual(resolved, ["id_2"])

    def test_selection_model_initial_selection(self) -> None:
        phones = [
            ("s1", "Phone 1"),
            ("s2", "Phone 2"),
            ("s3", "Phone 3"),
        ]
        model = PhoneSelectionModel(phones, selected_ids=["s3", "s1"])
        self.assertTrue(model.is_selected("s1"))
        self.assertFalse(model.is_selected("s2"))
        self.assertTrue(model.is_selected("s3"))
        self.assertEqual(model.get_selected_ids(), ["s1", "s3"])

    def test_selection_model_select_all(self) -> None:
        phones = [
            ("s1", "Phone 1"),
            ("s2", "Phone 2"),
        ]
        model = PhoneSelectionModel(phones, selected_ids=())
        self.assertEqual(model.get_selected_ids(), [])
        model.select_all()
        self.assertTrue(model.all_selected)
        self.assertEqual(model.get_selected_ids(), ["s1", "s2"])

    def test_selection_model_deselect_all(self) -> None:
        phones = [
            ("s1", "Phone 1"),
            ("s2", "Phone 2"),
        ]
        model = PhoneSelectionModel(phones, selected_ids=["s1", "s2"])
        self.assertTrue(model.all_selected)
        model.deselect_all()
        self.assertEqual(model.get_selected_ids(), [])
        self.assertFalse(model.all_selected)

    def test_selection_model_toggle_all(self) -> None:
        phones = [
            ("s1", "Phone 1"),
            ("s2", "Phone 2"),
        ]
        model = PhoneSelectionModel(phones, selected_ids=["s1"])
        # Не все выбраны -> toggle_all выбирает все
        model.toggle_all()
        self.assertEqual(model.get_selected_ids(), ["s1", "s2"])
        # Все выбраны -> toggle_all снимает все
        model.toggle_all()
        self.assertEqual(model.get_selected_ids(), [])

    def test_selection_model_toggle_individual(self) -> None:
        phones = [("s1", "Phone 1")]
        model = PhoneSelectionModel(phones)
        self.assertFalse(model.is_selected("s1"))
        toggled_on = model.toggle("s1")
        self.assertTrue(toggled_on)
        self.assertTrue(model.is_selected("s1"))
        toggled_off = model.toggle("s1")
        self.assertFalse(toggled_off)
        self.assertFalse(model.is_selected("s1"))

    def test_selection_model_empty_candidates(self) -> None:
        model = PhoneSelectionModel([], selected_ids=["s1", "s2"])
        self.assertTrue(model.is_empty)
        self.assertEqual(model.phones, [])
        self.assertEqual(model.get_selected_ids(), [])
        model.select_all()
        self.assertEqual(model.get_selected_ids(), [])


class TestDiagnosticPhonePickerMockedHeadless(unittest.TestCase):
    """Тесты контракта DiagnosticPhonePicker с замоканной графической подсистемой (headless)."""

    def setUp(self) -> None:
        # Мокаем ttk.Frame.__init__ и методы Tk
        self._frame_patch = patch("tkinter.ttk.Frame.__init__", return_value=None)
        self._frame_patch.start()
        self.addCleanup(self._frame_patch.stop)

    @patch("tkinter.ttk.Label")
    @patch("tkinter.ttk.Button")
    @patch("tkinter.ttk.Scrollbar")
    @patch("tkinter.Listbox")
    @patch("tkinter.ttk.Frame")
    def test_picker_contract_and_apply(
        self,
        mock_frame_cls: MagicMock,
        mock_listbox_cls: MagicMock,
        mock_scrollbar_cls: MagicMock,
        mock_button_cls: MagicMock,
        mock_label_cls: MagicMock,
    ) -> None:
        mock_parent = MagicMock()
        mock_listbox = MagicMock()
        mock_listbox.winfo_exists.return_value = True
        mock_listbox.curselection.return_value = (0, 2)
        mock_listbox_cls.return_value = mock_listbox

        applied_ids: list[list[str]] = []
        cancelled_calls: list[bool] = []

        raw_phones = [
            ("dev1", "Label 1"),
            ("dev2", "Label 2"),
            ("dev3", "Label 3"),
            ("dev1", "Duplicate Dev 1"),
        ]

        picker = DiagnosticPhonePicker(
            mock_parent,
            raw_phones,
            selected_ids=["dev3"],
            on_apply=lambda ids: applied_ids.append(ids),
            on_cancel=lambda: cancelled_calls.append(True),
        )

        # Проверка дедупликации
        self.assertEqual(len(picker.phones), 3)
        self.assertEqual(picker.phones[0][0], "dev1")
        self.assertEqual(picker.phones[1][0], "dev2")
        self.assertEqual(picker.phones[2][0], "dev3")

        # Проверка применения выбора
        picker.apply()
        self.assertEqual(len(applied_ids), 1)
        self.assertEqual(applied_ids[0], ["dev1", "dev3"])

        # Проверка отмены
        picker.cancel()
        self.assertEqual(cancelled_calls, [True])

    @patch("tkinter.ttk.Label")
    @patch("tkinter.ttk.Button")
    @patch("tkinter.ttk.Scrollbar")
    @patch("tkinter.Listbox")
    @patch("tkinter.ttk.Frame")
    def test_picker_empty_candidates_apply_empty(
        self,
        mock_frame_cls: MagicMock,
        mock_listbox_cls: MagicMock,
        mock_scrollbar_cls: MagicMock,
        mock_button_cls: MagicMock,
        mock_label_cls: MagicMock,
    ) -> None:
        mock_parent = MagicMock()
        mock_listbox = MagicMock()
        mock_listbox.winfo_exists.return_value = False
        mock_listbox_cls.return_value = mock_listbox

        applied: list[list[str]] = []
        picker = DiagnosticPhonePicker(
            mock_parent,
            [],
            selected_ids=["old_id"],
            on_apply=lambda ids: applied.append(ids),
            on_cancel=lambda: None,
        )

        self.assertEqual(picker.phones, [])
        self.assertEqual(picker.get_selected_ids(), [])

        # Применение пустого списка разрешено
        picker.apply()
        self.assertEqual(applied, [[]])

    @patch("tkinter.ttk.Label")
    @patch("tkinter.ttk.Button")
    @patch("tkinter.ttk.Scrollbar")
    @patch("tkinter.Listbox")
    @patch("tkinter.ttk.Frame")
    def test_sync_model_from_listbox_mocked(
        self,
        mock_frame_cls: MagicMock,
        mock_listbox_cls: MagicMock,
        mock_scrollbar_cls: MagicMock,
        mock_button_cls: MagicMock,
        mock_label_cls: MagicMock,
    ) -> None:
        mock_parent = MagicMock()
        mock_listbox = MagicMock()
        mock_listbox.winfo_exists.return_value = True
        mock_listbox.curselection.return_value = (1,)
        mock_listbox_cls.return_value = mock_listbox

        picker = DiagnosticPhonePicker(
            mock_parent,
            [("dev1", "Label 1"), ("dev2", "Label 2")],
            selected_ids=(),
            on_apply=lambda ids: None,
            on_cancel=lambda: None,
        )
        self.assertEqual(picker._model.get_selected_ids(), [])
        picker._sync_model_from_listbox()
        self.assertEqual(picker._model.get_selected_ids(), ["dev2"])


@unittest.skipUnless(HAS_DISPLAY, "Tk-тесты требуют активного дисплея X11/Wayland")
class TestDiagnosticPhonePickerTk(unittest.TestCase):
    """Интеграционные тесты графического компонента при наличии дисплея."""

    def setUp(self) -> None:
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_tk_widget_creation_and_preselection(self) -> None:
        applied_results: list[list[str]] = []
        phones = [
            ("sn_001", "Device One (sn_001)"),
            ("sn_002", "Device Two (sn_002)"),
            ("sn_003", "Device Three (sn_003)"),
        ]

        picker = DiagnosticPhonePicker(
            self.root,
            phones,
            selected_ids=["sn_002"],
            on_apply=lambda ids: applied_results.append(ids),
            on_cancel=lambda: None,
        )
        picker.pack()
        self.root.update()

        # Проверяем, что в списке отображаются готовые подписи
        self.assertEqual(picker.listbox.size(), 3)
        self.assertEqual(picker.listbox.get(0), "Device One (sn_001)")
        self.assertEqual(picker.listbox.get(1), "Device Two (sn_002)")
        self.assertEqual(picker.listbox.get(2), "Device Three (sn_003)")

        # Проверяем предвыбор
        self.assertEqual(picker.get_selected_ids(), ["sn_002"])

        # Вызываем Применить
        picker.apply_btn.invoke()
        self.assertEqual(applied_results, [["sn_002"]])

    def test_tk_deduplication_in_listbox(self) -> None:
        phones = [
            ("usb_1", "Phone 1"),
            ("usb_2", "Phone 2"),
            ("usb_1", "Duplicate Phone 1"),
        ]
        picker = DiagnosticPhonePicker(
            self.root,
            phones,
            on_apply=lambda ids: None,
            on_cancel=lambda: None,
        )
        picker.pack()
        self.root.update()

        self.assertEqual(picker.listbox.size(), 2)
        self.assertEqual(picker.listbox.get(0), "Phone 1")
        self.assertEqual(picker.listbox.get(1), "Phone 2")

    def test_tk_all_phones_button(self) -> None:
        phones = [
            ("p1", "Phone 1"),
            ("p2", "Phone 2"),
        ]
        picker = DiagnosticPhonePicker(
            self.root,
            phones,
            selected_ids=(),
            on_apply=lambda ids: None,
            on_cancel=lambda: None,
        )
        picker.pack()
        self.root.update()

        self.assertEqual(picker.get_selected_ids(), [])
        picker.all_button.invoke()
        self.root.update()
        self.assertEqual(picker.get_selected_ids(), ["p1", "p2"])

    def test_tk_cancel_button(self) -> None:
        cancelled = []
        picker = DiagnosticPhonePicker(
            self.root,
            [("p1", "Phone 1")],
            on_apply=lambda ids: None,
            on_cancel=lambda: cancelled.append(True),
        )
        picker.pack()
        self.root.update()

        picker.cancel_btn.invoke()
        self.assertEqual(cancelled, [True])

    def test_tk_empty_state_and_clear_choice(self) -> None:
        applied = []
        picker = DiagnosticPhonePicker(
            self.root,
            [],
            selected_ids=["stale_id"],
            on_apply=lambda ids: applied.append(ids),
            on_cancel=lambda: None,
        )
        picker.pack()
        self.root.update()

        # Показывается полезное сообщение
        self.assertIn(EMPTY_MESSAGE, picker.empty_label.cget("text"))
        self.assertIn(EMPTY_HINT, picker.empty_label.cget("text"))

        # Разрешено применить пустой список
        picker.apply_btn.invoke()
        self.assertEqual(applied, [[]])

    def test_tk_keyboard_return_and_escape(self) -> None:
        import tkinter as tk
        applied = []
        cancelled = []

        top = tk.Toplevel(self.root)
        picker = DiagnosticPhonePicker(
            top,
            [("p1", "Phone 1"), ("p2", "Phone 2")],
            selected_ids=["p1"],
            on_apply=lambda ids: applied.append(ids),
            on_cancel=lambda: cancelled.append(True),
        )
        picker.pack()
        top.update_idletasks()
        top.deiconify()
        top.focus_force()
        picker.listbox.focus_set()
        top.update()

        # Enter triggers apply
        picker.listbox.event_generate("<Return>")
        top.update()
        self.assertEqual(applied, [["p1"]])

        # Escape triggers cancel
        picker.listbox.event_generate("<Escape>")
        top.update()
        self.assertEqual(cancelled, [True])

        top.destroy()

    def test_tk_keyboard_space_toggles_selection(self) -> None:
        import tkinter as tk
        top = tk.Toplevel(self.root)
        picker = DiagnosticPhonePicker(
            top,
            [("p1", "Phone 1"), ("p2", "Phone 2")],
            selected_ids=(),
            on_apply=lambda ids: None,
            on_cancel=lambda: None,
        )
        picker.pack()
        top.update()

        picker.listbox.focus_set()
        picker.listbox.activate(1)
        top.update()

        # Пробел переключает элемент с индексом 1
        picker._on_space(None)
        self.assertEqual(picker.get_selected_ids(), ["p2"])

        # Повторный пробел снимает выбор
        picker._on_space(None)
        self.assertEqual(picker.get_selected_ids(), [])

        top.destroy()

    def test_tk_frame_does_not_bind_escape_to_parent_toplevel(self) -> None:
        import tkinter as tk
        top = tk.Toplevel(self.root)
        top.bind("<Escape>", lambda _e: None)
        bindings_before = top.bind("<Escape>")

        picker = DiagnosticPhonePicker(
            top,
            [("p1", "Phone 1")],
            on_apply=lambda ids: None,
            on_cancel=lambda: None,
        )
        picker.pack()
        top.update()

        bindings_after = top.bind("<Escape>")
        self.assertEqual(bindings_before, bindings_after)
        top.destroy()

    def test_tk_space_and_listbox_select_synchronizes_model(self) -> None:
        import tkinter as tk
        top = tk.Toplevel(self.root)
        picker = DiagnosticPhonePicker(
            top,
            [("p1", "Phone 1"), ("p2", "Phone 2")],
            selected_ids=(),
            on_apply=lambda ids: None,
            on_cancel=lambda: None,
        )
        picker.pack()
        top.update()

        self.assertEqual(picker._model.get_selected_ids(), [])

        # Переключение пробелом должно обновлять не только listbox, но и модель
        picker.listbox.focus_set()
        picker.listbox.activate(0)
        picker._on_space(None)
        top.update()

        self.assertEqual(picker.get_selected_ids(), ["p1"])
        self.assertEqual(picker._model.get_selected_ids(), ["p1"])
        self.assertTrue(picker._model.is_selected("p1"))

        # Выделение напрямую через ListboxSelect также обновляет модель
        picker.listbox.selection_set(1)
        picker.listbox.event_generate("<<ListboxSelect>>")
        top.update()

        self.assertEqual(picker.get_selected_ids(), ["p1", "p2"])
        self.assertEqual(picker._model.get_selected_ids(), ["p1", "p2"])
        self.assertTrue(picker._model.all_selected)

        # Проверяем, что toggle_all теперь корректно сработает с синхронизированной моделью
        picker.toggle_all()
        top.update()
        self.assertEqual(picker.get_selected_ids(), [])
        self.assertEqual(picker._model.get_selected_ids(), [])

        top.destroy()

    def test_tk_theme_fallback_and_integration(self) -> None:
        # Тестируем безопасный вызов API темы, если модуль theme присутствует
        dummy_theme = types.ModuleType("desktop.theme")
        applied_styles = []
        dummy_theme.apply_theme = lambda w: applied_styles.append("applied")
        dummy_theme.BG_PANEL = "#112233"
        dummy_theme.FG_PANEL = "#ffffff"

        with patch("desktop.diagnostic_phone_picker.theme", dummy_theme):
            picker = DiagnosticPhonePicker(
                self.root,
                [("dev1", "Dev 1")],
                on_apply=lambda ids: None,
                on_cancel=lambda: None,
            )
            picker.pack()
            self.root.update()

            self.assertEqual(applied_styles, ["applied"])
            self.assertEqual(picker.listbox.cget("bg"), "#112233")


if __name__ == "__main__":
    unittest.main()
