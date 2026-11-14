"""Тесты компонента DeviceLinkPanel (поддержка headless и Tk-окружений)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import tkinter as tk
from unittest.mock import MagicMock, patch

import pytest

from desktop.adb_client import DeviceInfo
from desktop.device_links import DeviceLinkConflictError, DeviceLinkError, LinkRegistry
from desktop.hub_client import HubDevice
from desktop.link_panel import HINT_NO_SERVICE, DeviceLinkPanel
from desktop.link_service import DeviceLinkService, LinkConflictError


def can_init_tk() -> bool:
    """Проверяет возможность создания реального Tk окна в текущем окружении."""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@pytest.fixture
def tk_root():
    """Фикстура Tk корня для сред с дисплеем без вызова update_idletasks при teardown."""
    if not can_init_tk():
        pytest.skip("Графический дисплей недоступен в текущей среде (headless)")
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def make_mock_master() -> MagicMock:
    """Создает мок-мастер для тестирования в headless-среде без X11 дисплея."""
    master = MagicMock(spec=tk.Widget)
    master._w = "."
    master.children = {}
    master._tclCommands = []
    master.tk = MagicMock()
    return master


@pytest.fixture
def temp_service():
    """Фикстура изолированного сервиса с временным файлом реестра."""
    with tempfile.TemporaryDirectory() as tmpdir:
        reg_file = Path(tmpdir) / "test_links.json"
        registry = LinkRegistry(reg_file)
        service = DeviceLinkService(registry=registry)
        yield service


def create_sample_usb(serial: str = "USB_DEVICE_1", model: str = "Pixel_8") -> DeviceInfo:
    """Вспомогательная функция создания готового USB-устройства."""
    return DeviceInfo(
        serial=serial,
        state="device",
        state_display="Готов к работе",
        is_network=False,
        connection_type="USB",
        is_ready=True,
        model=model,
        usb_info="usb:1-1",
    )


def create_sample_hub(hub_id: str = "hub_dev_1", name: str = "Remote Phone") -> HubDevice:
    """Вспомогательная функция создания устройства DroidHub."""
    return HubDevice(id=hub_id, name=name, online=True)


# ===========================================================================
# 1. Headless-тесты с моками (выполняются в любом окружении)
# ===========================================================================


def test_headless_panel_init_no_service():
    """Проверка начального состояния панели без переданного сервиса."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    assert panel.selected_usb_serial is None
    assert panel.selected_hub_id is None
    assert panel.selected_pair is None
    assert panel.is_link_enabled is False
    assert panel.is_unlink_enabled is False
    assert panel.current_hint == HINT_NO_SERVICE
    assert panel.current_error == ""
    assert len(panel.rendered_pairs) == 0

    # Проверка снятия дочерних виджетов
    children = panel.winfo_children()
    assert isinstance(children, list)


def test_headless_format_labels():
    """Проверка формирования понятных русских подписей для устройств."""
    usb_dev = create_sample_usb(serial="SERIAL123", model="Galaxy_S24")
    label_usb = DeviceLinkPanel.format_usb_label(usb_dev)
    assert "Galaxy S24" in label_usb
    assert "SERIAL123" in label_usb

    hub_dev = create_sample_hub(hub_id="hub-456", name="Тестовый стенд")
    label_hub = DeviceLinkPanel.format_hub_label(hub_dev)
    assert "Тестовый стенд" in label_hub
    assert "hub-456" in label_hub

    # Случай, когда имя совпадает с id
    hub_dev_same = HubDevice(id="device-999", name="device-999")
    label_hub_same = DeviceLinkPanel.format_hub_label(hub_dev_same)
    assert "device-999" in label_hub_same


def test_headless_set_context_no_preselection(temp_service):
    """При обновлении снимка устройства загружаются, но нет предвыбора."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb1 = create_sample_usb("USB_1", "Pixel_7")
    usb2 = create_sample_usb("USB_2", "Xiaomi")
    hub1 = create_sample_hub("HUB_1", "Remote_1")
    hub2 = create_sample_hub("HUB_2", "Remote_2")

    panel.set_context(
        service=temp_service,
        local_devices=[usb1, usb2],
        hub_devices=[hub1, hub2],
    )

    # Без предвыбора
    assert panel.selected_usb_serial is None
    assert panel.selected_hub_id is None
    assert panel.is_link_enabled is False
    assert panel.current_hint == ""

    # Проверяем, что оба USB и оба Hub доступны для выбора
    assert "USB_1" in panel._usb_serial_to_label
    assert "USB_2" in panel._usb_serial_to_label
    assert "HUB_1" in panel._hub_id_to_label
    assert "HUB_2" in panel._hub_id_to_label


def test_headless_explicit_selection_and_linking(temp_service):
    """Явный выбор по идентификатору и создание связи."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb = create_sample_usb("USB_A", "Pixel_8")
    hub = create_sample_hub("HUB_A", "Hub Pixel")

    panel.set_context(service=temp_service, local_devices=[usb], hub_devices=[hub])

    # Выбираем только USB — кнопка всё ещё отключена
    panel.select_usb("USB_A")
    assert panel.selected_usb_serial == "USB_A"
    assert panel.selected_hub_id is None
    assert panel.is_link_enabled is False

    # Выбираем Hub — теперь кнопка активна
    panel.select_hub("HUB_A")
    assert panel.selected_hub_id == "HUB_A"
    assert panel.is_link_enabled is True

    # Выполняем привязку
    ok = panel.link_selected_devices()
    assert ok is True
    assert panel.current_error == ""

    # После привязки выбор сброшен, кнопка отключена, а пара появилась в сохранённых
    assert panel.selected_usb_serial is None
    assert panel.selected_hub_id is None
    assert panel.is_link_enabled is False
    assert len(panel.rendered_pairs) == 1
    assert panel.rendered_pairs[0].serial == "USB_A"
    assert panel.rendered_pairs[0].hub_id == "HUB_A"

    # Теперь эти устройства не должны быть в доступных для новой привязки
    assert "USB_A" not in panel._usb_serial_to_label
    assert "HUB_A" not in panel._hub_id_to_label


def test_headless_selection_and_deletion_with_confirmation(temp_service):
    """Выбор сохранённой пары и удаление с подтверждением."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb = create_sample_usb("USB_B", "Pixel_8")
    hub = create_sample_hub("HUB_B", "Hub Pixel")
    panel.set_context(service=temp_service, local_devices=[usb], hub_devices=[hub])

    panel.select_usb("USB_B")
    panel.select_hub("HUB_B")
    panel.link_selected_devices()

    # Выбираем созданную пару
    panel.select_pair("USB_B", "HUB_B")
    assert panel.selected_pair == ("USB_B", "HUB_B")
    assert panel.is_unlink_enabled is True

    # 1. Отказ в подтверждении: связь не должна удаляться
    panel._confirm_callback = lambda s, h: False
    ok_cancel = panel.unlink_selected_pair()
    assert len(panel.rendered_pairs) == 1
    assert panel.selected_pair == ("USB_B", "HUB_B")
    assert panel.is_unlink_enabled is True

    # 2. Согласие в подтверждении: связь удаляется
    panel._confirm_callback = lambda s, h: True
    ok_delete = panel.unlink_selected_pair()
    assert ok_delete is True
    assert len(panel.rendered_pairs) == 0
    assert panel.selected_pair is None
    assert panel.is_unlink_enabled is False


def test_headless_deletion_when_phone_disconnected(temp_service):
    """Удаление привязанной пары работает, даже если телефон отключен."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb = create_sample_usb("OFFLINE_PHONE", "Samsung")
    hub = create_sample_hub("HUB_OFFLINE", "Remote")
    panel.set_context(service=temp_service, local_devices=[usb], hub_devices=[hub])
    panel.select_usb("OFFLINE_PHONE")
    panel.select_hub("HUB_OFFLINE")
    panel.link_selected_devices()

    # Теперь телефон физически отключили (local_devices пуст)
    panel.set_context(service=temp_service, local_devices=[], hub_devices=[hub])
    assert len(panel.rendered_pairs) == 1
    pair_status = panel.rendered_pairs[0]
    assert pair_status.local_present is False
    assert "отключен" in pair_status.status_summary

    # Выбираем пару и удаляем с подтверждением
    panel.select_pair("OFFLINE_PHONE", "HUB_OFFLINE")
    assert panel.is_unlink_enabled is True

    panel._confirm_callback = lambda s, h: True
    panel.unlink_selected_pair()

    assert len(panel.rendered_pairs) == 0
    assert panel.selected_pair is None


def test_headless_selection_persistence_on_same_context(temp_service):
    """Сохранение актуального выбора при обновлении тех же снимков и того же сервиса."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb1 = create_sample_usb("USB_KEEP", "Pixel_6")
    hub1 = create_sample_hub("HUB_KEEP", "Hub_6")

    panel.set_context(service=temp_service, local_devices=[usb1], hub_devices=[hub1])
    panel.select_usb("USB_KEEP")
    panel.select_hub("HUB_KEEP")
    assert panel.is_link_enabled is True

    # Повторный снимок с теми же устройствами и тем же сервисом
    panel.set_context(service=temp_service, local_devices=[usb1], hub_devices=[hub1])
    assert panel.selected_usb_serial == "USB_KEEP"
    assert panel.selected_hub_id == "HUB_KEEP"
    assert panel.is_link_enabled is True


def test_headless_selection_reset_when_object_disappears(temp_service):
    """Сброс выбора, когда выбранный объект исчез из снимка."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb1 = create_sample_usb("USB_WILL_DISAPPEAR", "Pixel_6")
    hub1 = create_sample_hub("HUB_STAYS", "Hub_6")

    panel.set_context(service=temp_service, local_devices=[usb1], hub_devices=[hub1])
    panel.select_usb("USB_WILL_DISAPPEAR")
    panel.select_hub("HUB_STAYS")

    # USB исчез
    panel.set_context(service=temp_service, local_devices=[], hub_devices=[hub1])
    assert panel.selected_usb_serial is None
    assert panel.selected_hub_id == "HUB_STAYS"
    assert panel.is_link_enabled is False

    # Теперь и Hub исчез
    panel.set_context(service=temp_service, local_devices=[], hub_devices=[])
    assert panel.selected_hub_id is None


def test_headless_selection_reset_on_service_change(temp_service):
    """Сброс всех выборов при смене экземпляра сервиса."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb1 = create_sample_usb("USB_S", "Pixel_5")
    hub1 = create_sample_hub("HUB_S", "Hub_5")

    panel.set_context(service=temp_service, local_devices=[usb1], hub_devices=[hub1])
    panel.select_usb("USB_S")
    panel.select_hub("HUB_S")
    assert panel.is_link_enabled is True

    # Создаём другой сервис
    with tempfile.TemporaryDirectory() as tmpdir:
        reg2 = LinkRegistry(Path(tmpdir) / "links2.json")
        service2 = DeviceLinkService(registry=reg2)

        panel.set_context(service=service2, local_devices=[usb1], hub_devices=[hub1])
        # Выбор обязан сброситься при смене сервиса
        assert panel.selected_usb_serial is None
        assert panel.selected_hub_id is None
        assert panel.is_link_enabled is False


def test_headless_service_none_disables_operations_and_hints():
    """При отсутствии контекста (service=None) операции отключаются и отображается подсказка."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb = create_sample_usb("USB_X", "Pixel")
    hub = create_sample_hub("HUB_X", "Hub")

    panel.set_context(service=None, local_devices=[usb], hub_devices=[hub])

    assert panel.current_hint == HINT_NO_SERVICE
    assert panel.is_link_enabled is False
    assert panel.is_unlink_enabled is False
    assert panel.selected_usb_serial is None
    assert panel.selected_hub_id is None
    assert panel.selected_pair is None
    assert len(panel.rendered_pairs) == 0


def test_headless_registry_error_handling_on_update(temp_service):
    """Ошибки реестра при обновлении снимка отображаются в UI без заморозки и модальных окон."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    # Имитируем сбой чтения реестра
    with patch.object(
        temp_service, "get_linked_pairs", side_effect=DeviceLinkError("Файл реестра повреждён")
    ):
        panel.set_context(service=temp_service, local_devices=[], hub_devices=[])
        assert "Файл реестра повреждён" in panel.current_error
        # Панель остаётся живой и не бросает исключений

    # Следующий успешный снимок очищает ошибку
    panel.set_context(service=temp_service, local_devices=[], hub_devices=[])
    assert panel.current_error == ""


def test_headless_link_conflict_error_displayed_in_ui(temp_service):
    """Конфликт связывания обрабатывается в UI понятной ошибкой без исключения."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb = create_sample_usb("USB_C", "Pixel")
    hub = create_sample_hub("HUB_C", "Hub")
    panel.set_context(service=temp_service, local_devices=[usb], hub_devices=[hub])
    panel.select_usb("USB_C")
    panel.select_hub("HUB_C")

    with patch.object(
        temp_service, "link", side_effect=LinkConflictError("Устройство уже связано")
    ):
        ok = panel.link_selected_devices()
        assert ok is False
        assert "Устройство уже связано" in panel.current_error


def test_headless_unlink_error_displayed_in_ui(temp_service):
    """Ошибка при удалении связи отображается в интерфейсе."""
    master = make_mock_master()
    panel = DeviceLinkPanel(master)

    usb = create_sample_usb("USB_ERR", "Pixel")
    hub = create_sample_hub("HUB_ERR", "Hub")
    panel.set_context(service=temp_service, local_devices=[usb], hub_devices=[hub])
    panel.select_usb("USB_ERR")
    panel.select_hub("HUB_ERR")
    panel.link_selected_devices()

    panel.select_pair("USB_ERR", "HUB_ERR")
    panel._confirm_callback = lambda s, h: True

    with patch.object(
        temp_service, "unlink", side_effect=DeviceLinkError("Ошибка записи на диск")
    ):
        ok = panel.unlink_selected_pair()
        assert ok is False
        assert "Ошибка записи на диск" in panel.current_error


# ===========================================================================
# 2. Tk-тесты с реальным окном (пропускаются, если дисплей недоступен)
# ===========================================================================


def test_tk_panel_lifecycle_and_widgets(tk_root, temp_service):
    """Интеграционный тест жизненного цикла панели в реальном Tk-окне."""
    panel = DeviceLinkPanel(tk_root)
    panel.pack(fill=tk.BOTH, expand=True)

    # Проверка снятия дочерних виджетов
    children = panel.winfo_children()
    assert len(children) >= 3

    usb1 = create_sample_usb("TK_USB_1", "Pixel_7")
    hub1 = create_sample_hub("TK_HUB_1", "Hub_1")

    panel.set_context(service=temp_service, local_devices=[usb1], hub_devices=[hub1])

    # Имитация выбора через Combobox
    assert panel._usb_combo.cget("values")
    panel._usb_combo.current(0)
    panel._usb_combo.event_generate("<<ComboboxSelected>>")
    assert panel.selected_usb_serial == "TK_USB_1"

    panel._hub_combo.current(0)
    panel._hub_combo.event_generate("<<ComboboxSelected>>")
    assert panel.selected_hub_id == "TK_HUB_1"

    # Вызов кнопки Привязать через invoke
    panel._link_button.invoke()
    assert len(panel.rendered_pairs) == 1

    # Выбор в Treeview
    iid = "TK_USB_1::TK_HUB_1"
    panel._tree.selection_set(iid)
    panel._tree.event_generate("<<TreeviewSelect>>")
    assert panel.selected_pair == ("TK_USB_1", "TK_HUB_1")

    # Удаление через invoke с подтверждением
    panel._confirm_callback = lambda s, h: True
    panel._unlink_button.invoke()
    assert len(panel.rendered_pairs) == 0
    assert panel.selected_pair is None
