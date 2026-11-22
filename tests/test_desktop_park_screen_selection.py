"""Тесты модуля desktop.park_screen_selection.

Покрывают следующие сценарии:
- Смешанные видимые/скрытые/не готовые устройства.
- Дубликаты в выборке.
- Нет подходящих устройств (пустой результат).
- Пустой выбор.
- Нет исключений (feedback пустой).
- Несогласованные входные данные (ready_usb говорит «готово», state «нет»).
- Четыре подходящих устройства — порядок сохраняется.
- Усечение отображаемых исключений с суммарным счётчиком.
"""

from __future__ import annotations

import unittest

from desktop.park_screen_selection import ScreenOpenPlan, plan_screen_open


class TestPlanScreenOpenEmptySelection(unittest.TestCase):
    """Пустой выбор → пустой план, пустой feedback."""

    def test_empty_selected(self) -> None:
        plan = plan_screen_open(
            [],
            visible_ids=["A", "B"],
            ready_usb_ids=["A", "B"],
            state_by_id={"A": "device", "B": "device"},
            names_by_id={},
        )
        self.assertIsInstance(plan, ScreenOpenPlan)
        self.assertEqual(plan.serials, ())
        self.assertEqual(plan.feedback, "")


class TestPlanScreenOpenNoOmissions(unittest.TestCase):
    """Все выбранные устройства открываются → feedback пустой."""

    def test_all_eligible_no_feedback(self) -> None:
        plan = plan_screen_open(
            ["A", "B"],
            visible_ids=["A", "B", "C"],
            ready_usb_ids=["A", "B"],
            state_by_id={"A": "device", "B": "device"},
            names_by_id={"A": "Pixel 6", "B": "Samsung S22"},
        )
        self.assertEqual(plan.serials, ("A", "B"))
        self.assertEqual(plan.feedback, "")

    def test_single_eligible_no_feedback(self) -> None:
        plan = plan_screen_open(
            ["X"],
            visible_ids=["X"],
            ready_usb_ids=["X"],
            state_by_id={"X": "device"},
            names_by_id={},
        )
        self.assertEqual(plan.serials, ("X",))
        self.assertEqual(plan.feedback, "")


class TestPlanScreenOpenDuplicates(unittest.TestCase):
    """Дубликаты в выборке удаляются, порядок первого вхождения сохраняется."""

    def test_duplicates_removed(self) -> None:
        plan = plan_screen_open(
            ["A", "B", "A", "B", "A"],
            visible_ids=["B", "A"],
            ready_usb_ids=["A", "B"],
            state_by_id={"A": "device", "B": "device"},
            names_by_id={},
        )
        # B идёт раньше в visible, но A встречается первым в выборке;
        # eligible сортируется по visible_order: B(0) < A(1).
        self.assertEqual(plan.serials, ("B", "A"))
        self.assertEqual(plan.feedback, "")


class TestPlanScreenOpenMixedVisibleHiddenNotReady(unittest.TestCase):
    """Смешанные видимые, скрытые и не готовые устройства."""

    def test_mixed_scenario(self) -> None:
        # A — видим и готов → открывается
        # B — скрыт (не в visible_ids) → исключён («скрыт фильтром»)
        # C — видим и готов по USB, но state=hub_only → исключён
        # D — видим, готов, но state=hub_only → исключён
        # Порядок исключённых: сначала видимые (C, D), потом скрытые (B).
        # Показываются первые 2: C и D → содержат причину hub_only;
        # B свёрнут в «и ещё 1».
        # Проверяем, что B-исключение учтено в счётчике и что hub_only видна.
        plan = plan_screen_open(
            ["A", "B", "C", "D"],
            visible_ids=["A", "C", "D"],
            ready_usb_ids=["A", "C"],
            state_by_id={"A": "device", "B": "device", "C": "hub_only", "D": "hub_only"},
            names_by_id={"A": "Pixel", "B": "Redmi", "C": "OnePlus", "D": "Realme"},
        )
        self.assertEqual(plan.serials, ("A",))
        self.assertIn("Пропущено 3", plan.feedback)
        # B скрыт → свёрнут в «и ещё 1»
        self.assertIn("и ещё 1", plan.feedback)
        # C и D → 'доступен только через Hub' (оба видимы и идут первыми)
        self.assertIn("доступен только через Hub", plan.feedback)


class TestPlanScreenOpenNoEligible(unittest.TestCase):
    """Нет подходящих устройств — serials пуст, feedback непуст."""

    def test_no_eligible(self) -> None:
        # A и B видимы, но offline → нет связи по USB; ready_usb пуст.
        plan = plan_screen_open(
            ["A", "B"],
            visible_ids=["A", "B"],
            ready_usb_ids=[],
            state_by_id={"A": "offline", "B": "disconnected"},
            names_by_id={"A": "Dev A", "B": "Dev B"},
        )
        self.assertEqual(plan.serials, ())
        self.assertTrue(plan.feedback.startswith("Пропущено 2"))
        self.assertIn("нет связи по USB", plan.feedback)


class TestPlanScreenOpenInconsistentInputs(unittest.TestCase):
    """Несогласованный ввод: ready_usb говорит «готово», state блокирует."""

    def test_hub_only_overrides_ready_usb(self) -> None:
        """hub_only не открывается, даже если caller передал его в ready_usb."""
        plan = plan_screen_open(
            ["HUB"],
            visible_ids=["HUB"],
            ready_usb_ids=["HUB"],           # caller говорит «готов»
            state_by_id={"HUB": "hub_only"}, # но state блокирует
            names_by_id={},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("доступен только через Hub", plan.feedback)

    def test_unauthorized_overrides_ready_usb(self) -> None:
        plan = plan_screen_open(
            ["U"],
            visible_ids=["U"],
            ready_usb_ids=["U"],
            state_by_id={"U": "unauthorized"},
            names_by_id={},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("подтвердите отладку USB", plan.feedback)

    def test_offline_overrides_ready_usb(self) -> None:
        plan = plan_screen_open(
            ["O"],
            visible_ids=["O"],
            ready_usb_ids=["O"],
            state_by_id={"O": "offline"},
            names_by_id={},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("нет связи по USB", plan.feedback)

    def test_disconnected_overrides_ready_usb(self) -> None:
        plan = plan_screen_open(
            ["D"],
            visible_ids=["D"],
            ready_usb_ids=["D"],
            state_by_id={"D": "disconnected"},
            names_by_id={},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("нет связи по USB", plan.feedback)

    def test_missing_state_treated_as_offline(self) -> None:
        """Устройства без записи в state_by_id считаются недоступными."""
        plan = plan_screen_open(
            ["GHOST"],
            visible_ids=["GHOST"],
            ready_usb_ids=[],
            state_by_id={},         # нет записи
            names_by_id={},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("нет связи по USB", plan.feedback)

    def test_missing_state_with_inconsistent_ready_usb(self) -> None:
        """Устройство без записи в state_by_id не открывается, даже если
        caller передал его в ready_usb_ids (несогласованный ввод)."""
        plan = plan_screen_open(
            ["GHOST"],
            visible_ids=["GHOST"],
            ready_usb_ids=["GHOST"],  # caller говорит «готов», но строки нет
            state_by_id={},            # нет записи → должно блокироваться
            names_by_id={},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("нет связи по USB", plan.feedback)


class TestPlanScreenOpenFourEligiblePreservingOrder(unittest.TestCase):
    """Четыре подходящих устройства — порядок visible сохраняется."""

    def test_four_eligible_visible_order(self) -> None:
        # Видимый порядок: W X Y Z; выбор в другом порядке.
        plan = plan_screen_open(
            ["Z", "X", "W", "Y"],
            visible_ids=["W", "X", "Y", "Z"],
            ready_usb_ids=["W", "X", "Y", "Z"],
            state_by_id={
                "W": "device",
                "X": "device",
                "Y": "device",
                "Z": "device",
            },
            names_by_id={},
        )
        # Все четыре открываются в visible-порядке.
        self.assertEqual(plan.serials, ("W", "X", "Y", "Z"))
        self.assertEqual(plan.feedback, "")


class TestPlanScreenOpenSummaryCap(unittest.TestCase):
    """Усечение списка исключений: показывается ≤ 2, остальные свёрнуты."""

    def test_three_excluded_shows_two_plus_rest(self) -> None:
        # Выбраны E1..E4; E1 видим и готов; E2, E3, E4 — не готовы, но видимы.
        plan = plan_screen_open(
            ["E1", "E2", "E3", "E4"],
            visible_ids=["E1", "E2", "E3", "E4"],
            ready_usb_ids=["E1"],
            state_by_id={
                "E1": "device",
                "E2": "device",
                "E3": "device",
                "E4": "device",
            },
            names_by_id={"E2": "Poco X5", "E3": "Mi 11", "E4": "Redmi 9"},
        )
        self.assertEqual(plan.serials, ("E1",))
        self.assertIn("Пропущено 3", plan.feedback)
        # Первые два из трёх исключённых показаны
        self.assertIn("Poco X5", plan.feedback)
        self.assertIn("Mi 11", plan.feedback)
        # Третий свёрнут
        self.assertIn("и ещё 1", plan.feedback)
        # Redmi 9 (E4) не должен упоминаться явно
        self.assertNotIn("Redmi 9", plan.feedback)

    def test_exactly_two_excluded_no_rest_suffix(self) -> None:
        """Ровно два исключённых — суффикс «и ещё» не нужен."""
        plan = plan_screen_open(
            ["G", "H"],
            visible_ids=["G", "H"],
            ready_usb_ids=[],
            state_by_id={"G": "device", "H": "device"},
            names_by_id={"G": "G-Phone", "H": "H-Phone"},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("Пропущено 2", plan.feedback)
        self.assertIn("G-Phone", plan.feedback)
        self.assertIn("H-Phone", plan.feedback)
        self.assertNotIn("и ещё", plan.feedback)


class TestPlanScreenOpenReturnType(unittest.TestCase):
    """Возвращаемое значение является frozen dataclass с правильными типами."""

    def test_return_is_frozen_dataclass(self) -> None:
        plan = plan_screen_open(
            ["X"],
            visible_ids=["X"],
            ready_usb_ids=["X"],
            state_by_id={"X": "device"},
            names_by_id={},
        )
        self.assertIsInstance(plan, ScreenOpenPlan)
        self.assertIsInstance(plan.serials, tuple)
        self.assertIsInstance(plan.feedback, str)
        # frozen → нельзя изменить
        with self.assertRaises(Exception):
            plan.serials = ()  # type: ignore[misc]


class TestPlanScreenOpenHiddenDeviceFeedbackPriority(unittest.TestCase):
    """Скрытое устройство с заблокированным состоянием → причина фильтра первая."""

    def test_hidden_hub_only_shows_filter_reason(self) -> None:
        # Устройство скрыто И имеет состояние hub_only → причина «скрыт фильтром»
        plan = plan_screen_open(
            ["HH"],
            visible_ids=[],     # не видно
            ready_usb_ids=[],
            state_by_id={"HH": "hub_only"},
            names_by_id={"HH": "HubPhone"},
        )
        self.assertEqual(plan.serials, ())
        self.assertIn("скрыт фильтром", plan.feedback)
        # Причина hub_only не показывается, т.к. скрыто — первый приоритет.
        self.assertNotIn("доступен только через Hub", plan.feedback)


class TestPlanScreenOpenStableExcludedOrder(unittest.TestCase):
    """Стабильный порядок исключённых: видимые по visible_order, скрытые по ID."""

    def test_visible_excluded_before_hidden_excluded(self) -> None:
        # C видим, но не готов; A и B скрыты.
        plan = plan_screen_open(
            ["A", "B", "C"],
            visible_ids=["C"],
            ready_usb_ids=[],
            state_by_id={"A": "device", "B": "device", "C": "device"},
            names_by_id={"A": "Alpha", "B": "Beta", "C": "Gamma"},
        )
        # C (видим) → USB не готов; A и B скрыты.
        # Первые два в сообщении: C (видимый) и A (первый из скрытых по алфавиту).
        self.assertIn("Gamma", plan.feedback)   # C (видим, первый)
        self.assertIn("Alpha", plan.feedback)   # A (скрытый, первый по алфавиту)
        self.assertIn("и ещё 1", plan.feedback) # B свёрнут


if __name__ == "__main__":
    unittest.main()
