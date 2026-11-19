"""Тесты автономного Tk/ttk компонента каталога скриптов ScriptCatalogView."""

from __future__ import annotations

import os
import subprocess
import tkinter as tk
from typing import Any
import unittest
from unittest.mock import patch

from desktop.script_catalog_view import (
    AUTOPILOT_ID,
    DEFAULT_CARDS,
    PHONE_DIAGNOSTICS_ID,
    ScriptCard,
    ScriptCatalogRegistry,
    ScriptCatalogView,
    _normalize_card,
)


def _can_create_display() -> bool:
    """Проверяет доступность графического дисплея Tk."""
    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


class TestScriptCatalogRegistry(unittest.TestCase):
    """Тесты реестра карточек сценариев, порядка и валидации ID."""

    def test_default_cards_order_and_ids(self) -> None:
        """По умолчанию ровно две карточки: Накрутка (autopilot) и Диагностика (phone_diagnostics)."""
        self.assertEqual(len(DEFAULT_CARDS), 2)

        card1, card2 = DEFAULT_CARDS
        self.assertEqual(card1.id, AUTOPILOT_ID)
        self.assertEqual(card1.id, "autopilot")
        self.assertEqual(card1.title, "Накрутка")
        self.assertIn("Автопилот", card1.description)

        self.assertEqual(card2.id, PHONE_DIAGNOSTICS_ID)
        self.assertEqual(card2.id, "phone_diagnostics")
        self.assertEqual(card2.title, "Диагностика телефона")
        self.assertIn("готовности", card2.description)
        self.assertIn("USB", card2.description)

        registry = ScriptCatalogRegistry()
        self.assertEqual(len(registry), 2)
        cards = registry.get_cards()
        self.assertEqual([c.id for c in cards], ["autopilot", "phone_diagnostics"])
        self.assertEqual([c.title for c in cards], ["Накрутка", "Диагностика телефона"])

    def test_registry_extension_via_extra_cards(self) -> None:
        """Реестр расширяется через keyword-only параметр extra_cards."""
        extra = [
            ScriptCard(
                id="custom_backup",
                title="Резервная копия",
                description="Создание бэкапа данных приложений",
            )
        ]
        registry = ScriptCatalogRegistry(extra_cards=extra)
        self.assertEqual(len(registry), 3)
        self.assertEqual(
            [c.id for c in registry.get_cards()],
            ["autopilot", "phone_diagnostics", "custom_backup"],
        )
        self.assertTrue(registry.has_id("custom_backup"))
        self.assertEqual(registry.get("custom_backup"), extra[0])

    def test_registry_extension_via_extra_scripts(self) -> None:
        """Реестр расширяется через keyword-only параметр extra_scripts."""
        extra = [
            {
                "id": "cleaner",
                "title": "Очистка кэша",
                "description": "Удаление временных файлов устройств",
            }
        ]
        registry = ScriptCatalogRegistry(extra_scripts=extra)
        self.assertEqual(len(registry), 3)
        card = registry.get("cleaner")
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.id, "cleaner")
        self.assertEqual(card.title, "Очистка кэша")
        self.assertEqual(card.description, "Удаление временных файлов устройств")

    def test_registry_custom_initial_cards(self) -> None:
        """Передача cards заменяет список по умолчанию."""
        custom = [
            ScriptCard(id="task_a", title="Сценарий A", description="Описание A"),
            ScriptCard(id="task_b", title="Сценарий B", description="Описание B"),
        ]
        registry = ScriptCatalogRegistry(cards=custom)
        self.assertEqual([c.id for c in registry], ["task_a", "task_b"])

    def test_registry_duplicate_id_raises_value_error(self) -> None:
        """Регистрация карточки с существующим ID вызывает ValueError."""
        registry = ScriptCatalogRegistry()
        # Дубликат встроенного ID
        with self.assertRaises(ValueError) as ctx:
            registry.register(
                ScriptCard(id="autopilot", title="Дубликат", description="Тест")
            )
        self.assertIn("autopilot", str(ctx.exception))

        # Дубликат при инициализации через extra_cards
        with self.assertRaises(ValueError):
            ScriptCatalogRegistry(
                extra_cards=[
                    ScriptCard(id="phone_diagnostics", title="Дубликат", description="Тест")
                ]
            )

        # Дубликат внутри кастомного списка
        with self.assertRaises(ValueError):
            ScriptCatalogRegistry(
                cards=[
                    ScriptCard(id="unique1", title="1", description="1"),
                    ScriptCard(id="unique1", title="1 dup", description="1 dup"),
                ]
            )

    def test_card_validation_empty_id_or_title(self) -> None:
        """Невалидный ID или заголовок вызывают ValueError."""
        with self.assertRaises(ValueError):
            ScriptCard(id="", title="Заголовок", description="Описание")
        with self.assertRaises(ValueError):
            ScriptCard(id="   ", title="Заголовок", description="Описание")
        with self.assertRaises(ValueError):
            ScriptCard(id="id1", title="", description="Описание")
        with self.assertRaises(ValueError):
            ScriptCard(id="id1", title="  ", description="Описание")
        with self.assertRaises(ValueError):
            ScriptCard(id="id1", title="Заголовок", description=123)  # type: ignore

    def test_normalize_card_formats(self) -> None:
        """Поддержка нормализации словарей и кортежей."""
        c1 = _normalize_card({"id": "d1", "title": "T1", "description": "D1"})
        self.assertEqual(c1, ScriptCard(id="d1", title="T1", description="D1"))

        c2 = _normalize_card(("d2", "T2", "D2"))
        self.assertEqual(c2, ScriptCard(id="d2", title="T2", description="D2"))

        with self.assertRaises(TypeError):
            _normalize_card(12345)  # type: ignore

    def test_registry_iteration_and_indexing(self) -> None:
        """Проверка интерфейса последовательности в реестре."""
        registry = ScriptCatalogRegistry()
        self.assertEqual(registry[0].id, "autopilot")
        self.assertEqual(registry[1].id, "phone_diagnostics")
        all_ids = [c.id for c in registry]
        self.assertEqual(all_ids, ["autopilot", "phone_diagnostics"])


class TestScriptCatalogViewUI(unittest.TestCase):
    """Тесты пользовательского интерфейса ScriptCatalogView в среде с графическим дисплеем."""

    def setUp(self) -> None:
        if not _can_create_display():
            raise unittest.SkipTest("Графический дисплей недоступен в текущей среде (headless)")

        self.root = tk.Tk()
        self.root.geometry("940x540")
        self.root.deiconify()
        self.root.focus_force()
        self.root.wait_visibility()
        self.opened_ids: list[str] = []

    def tearDown(self) -> None:
        if hasattr(self, "root") and self.root:
            try:
                self.root.destroy()
            except Exception:
                pass

    def _on_open_callback(self, script_id: str) -> None:
        self.opened_ids.append(script_id)

    def test_invalid_on_open_raises_type_error(self) -> None:
        """Передача невызываемого on_open вызывает TypeError."""
        with self.assertRaises(TypeError):
            ScriptCatalogView(self.root, on_open="not_a_callable")  # type: ignore

    def test_ui_default_cards_and_order(self) -> None:
        """Виджет каталога по умолчанию отображает карточки autopilot и phone_diagnostics."""
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        cards = view.cards
        self.assertEqual(len(cards), 2)
        self.assertEqual([c.id for c in cards], [AUTOPILOT_ID, PHONE_DIAGNOSTICS_ID])

        btn_auto = view.get_button(AUTOPILOT_ID)
        btn_diag = view.get_button(PHONE_DIAGNOSTICS_ID)
        self.assertIsNotNone(btn_auto)
        self.assertIsNotNone(btn_diag)

        frame_auto = view.get_card_frame(AUTOPILOT_ID)
        frame_diag = view.get_card_frame(PHONE_DIAGNOSTICS_ID)
        self.assertIsNotNone(frame_auto)
        self.assertIsNotNone(frame_diag)

    def test_ui_extension_via_extra_cards(self) -> None:
        """Виджет поддерживает добавление карточек через extra_cards и extra_scripts."""
        extra = [
            ScriptCard(
                id="custom_runner",
                title="Тестовый раннер",
                description="Запуск дополнительных проверок",
            )
        ]
        view = ScriptCatalogView(
            self.root,
            on_open=self._on_open_callback,
            extra_cards=extra,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        self.assertEqual(len(view.cards), 3)
        self.assertIsNotNone(view.get_button("custom_runner"))

    def test_ui_duplicate_id_raises_error(self) -> None:
        """Передача дубликата ID через extra_cards в виджет вызывает ValueError."""
        with self.assertRaises(ValueError):
            ScriptCatalogView(
                self.root,
                on_open=self._on_open_callback,
                extra_cards=[
                    ScriptCard(id=AUTOPILOT_ID, title="Дубликат", description="Тест")
                ],
            )

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("os.system")
    def test_single_event_on_button_click_no_command_executed(
        self,
        mock_system: Any,
        mock_popen: Any,
        mock_run: Any,
    ) -> None:
        """Клик по кнопке вызывает on_open ровно один раз без запуска системных команд."""
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        btn_auto = view.get_button(AUTOPILOT_ID)
        assert btn_auto is not None
        btn_auto.invoke()
        self.assertEqual(self.opened_ids, [AUTOPILOT_ID])
        self.assertEqual(view.last_opened_id, AUTOPILOT_ID)

        btn_diag = view.get_button(PHONE_DIAGNOSTICS_ID)
        assert btn_diag is not None
        btn_diag.invoke()
        self.assertEqual(self.opened_ids, [AUTOPILOT_ID, PHONE_DIAGNOSTICS_ID])
        self.assertEqual(view.last_opened_id, PHONE_DIAGNOSTICS_ID)

        # Проверка, что компонент не запускает системных процессов
        mock_system.assert_not_called()
        mock_popen.assert_not_called()
        mock_run.assert_not_called()

    def test_single_event_on_card_body_click(self) -> None:
        """Клик по телу карточки вызывает on_open ровно один раз."""
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        frame_diag = view.get_card_frame(PHONE_DIAGNOSTICS_ID)
        assert frame_diag is not None
        frame_diag.event_generate("<Button-1>")
        self.root.update()

        self.assertEqual(self.opened_ids, [PHONE_DIAGNOSTICS_ID])
        self.assertEqual(view.last_opened_id, PHONE_DIAGNOSTICS_ID)

    def test_single_event_on_key_press(self) -> None:
        """Активация по клавишам Return, KP_Enter, Space вызывает on_open ровно один раз."""
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        btn_auto = view.get_button(AUTOPILOT_ID)
        assert btn_auto is not None
        btn_auto.focus_force()
        self.root.update()

        btn_auto.event_generate("<Return>")
        self.root.update()
        self.assertEqual(self.opened_ids, [AUTOPILOT_ID])

        btn_diag = view.get_button(PHONE_DIAGNOSTICS_ID)
        assert btn_diag is not None
        btn_diag.focus_force()
        self.root.update()

        btn_diag.event_generate("<KP_Enter>")
        self.root.update()
        self.assertEqual(self.opened_ids, [AUTOPILOT_ID, PHONE_DIAGNOSTICS_ID])

        # Space key
        btn_diag.event_generate("<KeyPress-space>")
        btn_diag.event_generate("<KeyRelease-space>")
        self.root.update()
        self.assertEqual(
            self.opened_ids,
            [AUTOPILOT_ID, PHONE_DIAGNOSTICS_ID, PHONE_DIAGNOSTICS_ID],
        )

    def test_keyboard_navigation_between_cards(self) -> None:
        """Стрелки вправо/вниз и влево/вверх перемещают фокус между карточками."""
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        btn_auto = view.get_button(AUTOPILOT_ID)
        btn_diag = view.get_button(PHONE_DIAGNOSTICS_ID)
        assert btn_auto is not None
        assert btn_diag is not None

        # Начальный фокус на первой карточке
        btn_auto.focus_force()
        self.root.update()

        # Стрелка вправо -> фокус на второй карточке
        btn_auto.event_generate("<Right>")
        self.root.update()
        self.assertEqual(view.focused_card_id, PHONE_DIAGNOSTICS_ID)
        self.assertEqual(self.root.focus_get(), btn_diag)

        # Стрелка влево -> возврат к первой карточке
        btn_diag.event_generate("<Left>")
        self.root.update()
        self.assertEqual(view.focused_card_id, AUTOPILOT_ID)
        self.assertEqual(self.root.focus_get(), btn_auto)

        # Home / End
        btn_auto.event_generate("<End>")
        self.root.update()
        self.assertEqual(view.focused_card_id, PHONE_DIAGNOSTICS_ID)

        btn_diag.event_generate("<Home>")
        self.root.update()
        self.assertEqual(view.focused_card_id, AUTOPILOT_ID)

    def test_show_catalog_focus_restoration(self) -> None:
        """show_catalog() восстанавливает фокус на карточке, из которой была открыта панель."""
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        btn_auto = view.get_button(AUTOPILOT_ID)
        btn_diag = view.get_button(PHONE_DIAGNOSTICS_ID)
        assert btn_auto is not None
        assert btn_diag is not None

        # Начальный возврат к каталогу фокусирует первую карточку
        focused = view.show_catalog()
        self.root.update()
        self.assertEqual(focused, AUTOPILOT_ID)
        self.assertEqual(view.focused_card_id, AUTOPILOT_ID)
        self.assertEqual(self.root.focus_get(), btn_auto)

        # Пользователь открывает вторую карточку
        btn_diag.invoke()
        self.assertEqual(view.last_opened_id, PHONE_DIAGNOSTICS_ID)

        # Симуляция скрытия каталога и возврата из панели сценария
        view.pack_forget()
        self.root.update()

        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        # Вызов show_catalog() возвращает фокус на phone_diagnostics
        restored = view.show_catalog()
        self.root.update()
        self.assertEqual(restored, PHONE_DIAGNOSTICS_ID)
        self.assertEqual(view.focused_card_id, PHONE_DIAGNOSTICS_ID)
        self.assertEqual(self.root.focus_get(), btn_diag)

        # Явный вызов show_catalog с другим ID
        explicit = view.show_catalog(AUTOPILOT_ID)
        self.root.update()
        self.assertEqual(explicit, AUTOPILOT_ID)
        self.assertEqual(view.focused_card_id, AUTOPILOT_ID)
        self.assertEqual(self.root.focus_get(), btn_auto)

    def test_compact_layout_on_940x540_no_clipping(self) -> None:
        """Компонент компактно располагается на 940x540 без обрезания карточек."""
        self.root.geometry("940x540")
        view = ScriptCatalogView(self.root, on_open=self._on_open_callback)
        view.pack(fill=tk.BOTH, expand=True)
        self.root.update()

        f_auto = view.get_card_frame(AUTOPILOT_ID)
        f_diag = view.get_card_frame(PHONE_DIAGNOSTICS_ID)
        assert f_auto is not None
        assert f_diag is not None

        # Размеры карточек не нулевые и вписываются в окно
        self.assertGreater(f_auto.winfo_width(), 200)
        self.assertGreater(f_auto.winfo_height(), 80)
        self.assertGreater(f_diag.winfo_width(), 200)
        self.assertGreater(f_diag.winfo_height(), 80)

        # Координаты нижних границ карточек строго внутри высоты 540
        auto_bottom = f_auto.winfo_y() + f_auto.winfo_height()
        diag_bottom = f_diag.winfo_y() + f_diag.winfo_height()
        self.assertLess(auto_bottom, 540)
        self.assertLess(diag_bottom, 540)


if __name__ == "__main__":
    unittest.main()
