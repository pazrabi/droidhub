"""Осмысленные автономные тесты для desktop/link_service.py.

Проверяют:
1. Явный выбор: сопоставление только по выбранным serial и hub_id, без использования имен/моделей.
2. Неоднозначности: выявление дубликатов в списках локальных устройств и устройств хаба.
3. Неподходящие USB: строгий отказ для сетевых устройств (is_network), без usb_info, в не-device состояниях.
4. Отключённые USB: отказ при отсутствии серийного номера в списке локальных устройств.
5. Несуществующие hub_id: отказ при отсутствии hub_id в списке устройств хаба.
6. Конфликты 1:1: понятные сообщения об ошибках, невозможность привязать одно устройство к двум партнерам.
7. Отвязка: только явная, проверка по serial, hub_id и обоим параметрам, отсутствие неявных отвязок.
8. Наблюдение статусов: проверка присутствия и готовности сторон без сетевых запросов и действий.
9. Изоляция: гарантия отсутствия обращений к сети, subprocess и реальным устройствам.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from desktop.adb_client import DeviceInfo
from desktop.device_links import (
    DeviceLink,
    DeviceLinkConflictError,
    DeviceLinkValidationError,
    LinkRegistry,
)
from desktop.hub_client import HubDevice
from desktop.link_service import (
    DeviceAmbiguityError,
    DeviceLinkService,
    DeviceNotFoundError,
    IncompatibleDeviceError,
    LinkConflictError,
    LinkServiceError,
    LinkValidationError,
    LinkedPairStatus,
    check_usb_device_eligibility,
    format_pair_summary,
    get_linked_pairs,
    get_pair_status,
    inspect_linked_pairs,
    inspect_pair_status,
    is_valid_usb_device,
    link_devices,
    unlink_device,
)


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры и генераторы данных
# ---------------------------------------------------------------------------


def make_usb_device(
    serial: str = "USB_SERIAL_001",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    model: str | None = "Pixel 7a",
    product: str | None = "lynx",
    device_code: str | None = "lynx",
) -> DeviceInfo:
    """Создает валидный тестовый объект DeviceInfo для USB Android-устройства."""
    is_ready = (state.lower().strip() == "device")
    conn_type = "Сетевое (ADB TCP/IP)" if is_network else "USB"
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if is_ready else f"Статус: {state}",
        is_network=is_network,
        connection_type=conn_type,
        is_ready=is_ready,
        model=model,
        product=product,
        device_code=device_code,
        usb_info=usb_info,
    )


def make_hub_device(
    hub_id: str = "hub_phone_01",
    name: str = "Hub Pixel 7a",
    online: bool = True,
    battery: int | None = 85,
) -> HubDevice:
    """Создает валидный тестовый объект HubDevice."""
    return HubDevice(
        id=hub_id,
        name=name,
        online=online,
        battery=battery,
    )


@pytest.fixture
def registry(tmp_path: Path) -> LinkRegistry:
    """Создает изолированный временный реестр связей."""
    return LinkRegistry(tmp_path / "device_links.json")


@pytest.fixture
def service(registry: LinkRegistry) -> DeviceLinkService:
    """Создает изолированный экземпляр сервиса привязки."""
    return DeviceLinkService(registry=registry)


# ---------------------------------------------------------------------------
# 1. Тесты явного выбора (Explicit Linking)
# ---------------------------------------------------------------------------


class TestExplicitLinking:
    """Проверка явного выбора устройств по serial и hub_id."""

    def test_explicit_link_success(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Успешное явное связывание валидного USB-устройства с устройством хаба."""
        dev = make_usb_device("SERIAL_123", usb_info="1-1.2")
        hdev = make_hub_device("hub_dev_1")

        link = service.link(
            serial="SERIAL_123",
            hub_id="hub_dev_1",
            local_devices=[dev],
            hub_devices=[hdev],
        )

        assert isinstance(link, DeviceLink)
        assert link.serial == "SERIAL_123"
        assert link.hub_id == "hub_dev_1"
        assert registry.get_hub_id("SERIAL_123") == "hub_dev_1"
        assert registry.get_serial("hub_dev_1") == "SERIAL_123"

    def test_link_idempotent_same_pair(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Повторный вызов с идентичной парой идемпотентен и успешен."""
        dev = make_usb_device("SERIAL_123", usb_info="1-1.2")
        hdev = make_hub_device("hub_dev_1")

        link1 = service.link("SERIAL_123", "hub_dev_1", [dev], [hdev])
        link2 = service.link("SERIAL_123", "hub_dev_1", [dev], [hdev])

        assert link1 == link2
        assert len(registry.list()) == 1

    def test_names_and_models_are_not_used_for_automatching(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Имена и модели строго игнорируются: сопоставление возможно только по переданным serial/hub_id.

        Даже если у устройств одинаковая модель или имя, попытка связать несуществующий
        серийник или ID хаба не пытается «догадаться» по имени.
        """
        dev = make_usb_device("REAL_SERIAL_A", model="Pixel 7a", product="lynx")
        hdev = make_hub_device("hub_id_x", name="Pixel 7a")

        # Передача модели вместо serial вызывает DeviceNotFoundError, а не автосопоставление
        with pytest.raises(DeviceNotFoundError, match="WRONG_SERIAL"):
            service.link("WRONG_SERIAL", "hub_id_x", [dev], [hdev])

        # Передача имени вместо hub_id вызывает DeviceNotFoundError
        with pytest.raises(DeviceNotFoundError, match="Pixel 7a"):
            service.link("REAL_SERIAL_A", "Pixel 7a", [dev], [hdev])

        assert len(registry.list()) == 0

    def test_module_level_link_function(self, registry: LinkRegistry) -> None:
        """Публичная функция link_devices на уровне модуля работает корректно."""
        dev = make_usb_device("USB_999", usb_info="1-2")
        hdev = make_hub_device("hub_999")

        link = link_devices("USB_999", "hub_999", [dev], [hdev], registry)
        assert link.serial == "USB_999"
        assert link.hub_id == "hub_999"

    def test_service_with_preconfigured_devices(
        self, registry: LinkRegistry
    ) -> None:
        """Сервис с преднастроенными списками устройств выполняет link без повторной передачи списков."""
        dev = make_usb_device("SERIAL_PRE", usb_info="1-3")
        hdev = make_hub_device("hub_pre")
        srv = DeviceLinkService(registry=registry, local_devices=[dev], hub_devices=[hdev])

        link = srv.link("SERIAL_PRE", "hub_pre")
        assert link.serial == "SERIAL_PRE"
        assert link.hub_id == "hub_pre"


# ---------------------------------------------------------------------------
# 2. Тесты неоднозначностей (Ambiguities)
# ---------------------------------------------------------------------------


class TestAmbiguities:
    """Проверка защиты от неоднозначностей (дубликаты в списках устройств)."""

    def test_duplicate_local_serials_raises_ambiguity_error(
        self, service: DeviceLinkService
    ) -> None:
        """Несколько устройств с одинаковым serial вызывают DeviceAmbiguityError."""
        dev1 = make_usb_device("DUPLICATE_SERIAL", usb_info="1-1.1", model="Phone A")
        dev2 = make_usb_device("DUPLICATE_SERIAL", usb_info="1-1.2", model="Phone B")
        hdev = make_hub_device("hub_single")

        with pytest.raises(DeviceAmbiguityError, match="Неоднозначность.*2 записей"):
            service.link("DUPLICATE_SERIAL", "hub_single", [dev1, dev2], [hdev])

    def test_duplicate_hub_ids_raises_ambiguity_error(
        self, service: DeviceLinkService
    ) -> None:
        """Несколько устройств хаба с одинаковым ID вызывают DeviceAmbiguityError."""
        dev = make_usb_device("SERIAL_OK", usb_info="1-1")
        hdev1 = make_hub_device("DUPLICATE_HUB", name="Phone 1")
        hdev2 = make_hub_device("DUPLICATE_HUB", name="Phone 2")

        with pytest.raises(DeviceAmbiguityError, match="Неоднозначность.*2 записей"):
            service.link("SERIAL_OK", "DUPLICATE_HUB", [dev], [hdev1, hdev2])

    def test_ambiguity_inherits_validation_error(self) -> None:
        """DeviceAmbiguityError является подклассом LinkValidationError."""
        assert issubclass(DeviceAmbiguityError, LinkValidationError)
        assert issubclass(DeviceAmbiguityError, DeviceLinkValidationError)


# ---------------------------------------------------------------------------
# 3. Тесты строгой проверки USB (Incompatible Devices)
# ---------------------------------------------------------------------------


class TestIncompatibleUSBDevices:
    """Проверка строгого отклонения неподходящих или не-USB устройств."""

    def test_reject_network_flag_true(self, service: DeviceLinkService) -> None:
        """Устройство с флагом is_network=True отклоняется."""
        net_dev = make_usb_device("DEV_NET", is_network=True, usb_info="1-1")
        hdev = make_hub_device("hub_1")

        with pytest.raises(IncompatibleDeviceError, match="является сетевым"):
            service.link("DEV_NET", "hub_1", [net_dev], [hdev])

    def test_reject_network_serial_ip_port(self, service: DeviceLinkService) -> None:
        """Устройство с сетевым серийным номером (IP:port) отклоняется."""
        net_dev = make_usb_device("192.168.1.50:5555", is_network=False, usb_info="1-1")
        hdev = make_hub_device("hub_1")

        with pytest.raises(IncompatibleDeviceError, match="является сетевым"):
            service.link("192.168.1.50:5555", "hub_1", [net_dev], [hdev])

    def test_reject_missing_usb_info_none(self, service: DeviceLinkService) -> None:
        """Устройство без usb_info (None) отклоняется."""
        no_usb = make_usb_device("SERIAL_NO_USB", usb_info=None)
        hdev = make_hub_device("hub_1")

        with pytest.raises(IncompatibleDeviceError, match="отсутствует или пусто свойство 'usb_info'"):
            service.link("SERIAL_NO_USB", "hub_1", [no_usb], [hdev])

    def test_reject_empty_usb_info_string(self, service: DeviceLinkService) -> None:
        """Устройство с пустым usb_info отклоняется."""
        empty_usb = make_usb_device("SERIAL_EMPTY_USB", usb_info="   ")
        hdev = make_hub_device("hub_1")

        with pytest.raises(IncompatibleDeviceError, match="отсутствует или пусто свойство 'usb_info'"):
            service.link("SERIAL_EMPTY_USB", "hub_1", [empty_usb], [hdev])

    @pytest.mark.parametrize(
        "bad_state",
        [
            "unauthorized",
            "offline",
            "no permissions",
            "authorizing",
            "recovery",
            "sideload",
            "unknown",
        ],
    )
    def test_reject_non_device_state(
        self, service: DeviceLinkService, bad_state: str
    ) -> None:
        """Устройства в состояниях, отличных от 'device', строго отклоняются."""
        bad_dev = make_usb_device("DEV_BAD_STATE", state=bad_state, usb_info="1-1")
        hdev = make_hub_device("hub_1")

        with pytest.raises(IncompatibleDeviceError, match="требуется 'device'"):
            service.link("DEV_BAD_STATE", "hub_1", [bad_dev], [hdev])

    def test_reject_is_ready_false(self, service: DeviceLinkService) -> None:
        """Устройство с is_ready=False отклоняется даже при state='device'."""
        dev = make_usb_device("DEV_NOT_READY", state="device", usb_info="1-1")
        dev.is_ready = False
        hdev = make_hub_device("hub_1")

        with pytest.raises(IncompatibleDeviceError, match="не готово к работе"):
            service.link("DEV_NOT_READY", "hub_1", [dev], [hdev])

    def test_is_valid_usb_device_helper(self) -> None:
        """Функция is_valid_usb_device корректно возвращает True/False без исключений."""
        good = make_usb_device("GOOD_DEV", usb_info="1-1")
        assert is_valid_usb_device(good) is True

        net = make_usb_device("NET_DEV", is_network=True, usb_info="1-1")
        assert is_valid_usb_device(net) is False

        no_usb = make_usb_device("NO_USB", usb_info=None)
        assert is_valid_usb_device(no_usb) is False

        offline = make_usb_device("OFFLINE", state="offline", usb_info="1-1")
        assert is_valid_usb_device(offline) is False


# ---------------------------------------------------------------------------
# 4. Тесты отключённых USB и несуществующих hub_id
# ---------------------------------------------------------------------------


class TestDisconnectedAndNonExistentDevices:
    """Проверка обработки отключенных USB и отсутствующих hub_id."""

    def test_disconnected_usb_raises_device_not_found(
        self, service: DeviceLinkService
    ) -> None:
        """Серийный номер отсутствует в local_devices -> DeviceNotFoundError."""
        other_dev = make_usb_device("PRESENT_SERIAL", usb_info="1-1")
        hdev = make_hub_device("hub_1")

        with pytest.raises(DeviceNotFoundError, match="не найдено в списке подключенных"):
            service.link("DISCONNECTED_SERIAL", "hub_1", [other_dev], [hdev])

    def test_empty_local_device_list_raises_device_not_found(
        self, service: DeviceLinkService
    ) -> None:
        """Пустой список local_devices -> DeviceNotFoundError."""
        hdev = make_hub_device("hub_1")

        with pytest.raises(DeviceNotFoundError, match="не найдено в списке"):
            service.link("SOME_SERIAL", "hub_1", [], [hdev])

    def test_nonexistent_hub_id_raises_device_not_found(
        self, service: DeviceLinkService
    ) -> None:
        """hub_id отсутствует в hub_devices -> DeviceNotFoundError."""
        dev = make_usb_device("USB_DEV_1", usb_info="1-1")
        other_hdev = make_hub_device("hub_present")

        with pytest.raises(DeviceNotFoundError, match="не найдено в загруженном списке устройств хаба"):
            service.link("USB_DEV_1", "NON_EXISTENT_HUB", [dev], [other_hdev])

    def test_empty_hub_device_list_raises_device_not_found(
        self, service: DeviceLinkService
    ) -> None:
        """Пустой список hub_devices -> DeviceNotFoundError."""
        dev = make_usb_device("USB_DEV_1", usb_info="1-1")

        with pytest.raises(DeviceNotFoundError, match="не найдено в загруженном списке"):
            service.link("USB_DEV_1", "hub_1", [dev], [])


# ---------------------------------------------------------------------------
# 5. Тесты конфликтов 1:1 и понятных сообщений
# ---------------------------------------------------------------------------


class TestRegistryConflicts:
    """Проверка соблюдения отношения 1:1 и понятных сообщений об ошибках."""

    def test_conflict_serial_already_linked_to_different_hub(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Один USB не может быть привязан ко второму hub_id: информативное сообщение."""
        dev = make_usb_device("USB_SERIAL_1", usb_info="1-1")
        hub1 = make_hub_device("hub_id_1")
        hub2 = make_hub_device("hub_id_2")

        # Привязываем к hub_id_1
        service.link("USB_SERIAL_1", "hub_id_1", [dev], [hub1, hub2])

        # Попытка привязать тот же USB к hub_id_2
        with pytest.raises(LinkConflictError) as exc_info:
            service.link("USB_SERIAL_1", "hub_id_2", [dev], [hub1, hub2])

        err_msg = str(exc_info.value)
        assert "USB_SERIAL_1" in err_msg
        assert "hub_id_1" in err_msg
        assert "hub_id_2" in err_msg
        assert "unlink" in err_msg.lower() or "отвязк" in err_msg.lower()

    def test_conflict_hub_already_linked_to_different_serial(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Один hub_id не может быть привязан ко второму USB: информативное сообщение."""
        dev1 = make_usb_device("USB_A", usb_info="1-1")
        dev2 = make_usb_device("USB_B", usb_info="1-2")
        hub = make_hub_device("hub_shared")

        # Привязываем USB_A к hub_shared
        service.link("USB_A", "hub_shared", [dev1, dev2], [hub])

        # Попытка привязать USB_B к тому же hub_shared
        with pytest.raises(LinkConflictError) as exc_info:
            service.link("USB_B", "hub_shared", [dev1, dev2], [hub])

        err_msg = str(exc_info.value)
        assert "hub_shared" in err_msg
        assert "USB_A" in err_msg
        assert "USB_B" in err_msg
        assert "unlink" in err_msg.lower() or "отвязк" in err_msg.lower()

    def test_conflict_error_inherits_device_link_conflict_error(self) -> None:
        """LinkConflictError является подклассом DeviceLinkConflictError."""
        assert issubclass(LinkConflictError, DeviceLinkConflictError)
        assert issubclass(LinkConflictError, LinkServiceError)

    def test_conflict_does_not_mutate_registry(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Попытка создания конфликтной связи не повреждает и не изменяет существующий реестр."""
        dev1 = make_usb_device("USB_1", usb_info="1-1")
        dev2 = make_usb_device("USB_2", usb_info="1-2")
        hub1 = make_hub_device("hub_1")
        hub2 = make_hub_device("hub_2")

        service.link("USB_1", "hub_1", [dev1, dev2], [hub1, hub2])
        assert len(registry.list()) == 1

        with pytest.raises(LinkConflictError):
            service.link("USB_1", "hub_2", [dev1, dev2], [hub1, hub2])

        assert len(registry.list()) == 1
        assert registry.get_hub_id("USB_1") == "hub_1"


# ---------------------------------------------------------------------------
# 6. Тесты явной отвязки (Unlinking)
# ---------------------------------------------------------------------------


class TestExplicitUnlink:
    """Проверка строго явной отвязки устройств."""

    def test_unlink_by_serial(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Явная отвязка по серийному номеру."""
        dev = make_usb_device("USB_X", usb_info="1-1")
        hub = make_hub_device("hub_X")
        service.link("USB_X", "hub_X", [dev], [hub])

        assert registry.get_hub_id("USB_X") == "hub_X"

        res = service.unlink(serial="USB_X")
        assert res is True
        assert registry.get_hub_id("USB_X") is None
        assert registry.get_serial("hub_X") is None
        assert len(registry.list()) == 0

    def test_unlink_by_hub_id(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Явная отвязка по идентификатору хаба."""
        dev = make_usb_device("USB_Y", usb_info="1-1")
        hub = make_hub_device("hub_Y")
        service.link("USB_Y", "hub_Y", [dev], [hub])

        res = service.unlink(hub_id="hub_Y")
        assert res is True
        assert len(registry.list()) == 0

    def test_unlink_by_both_matching(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Явная отвязка с указанием обоих совпадающих параметров."""
        dev = make_usb_device("USB_Z", usb_info="1-1")
        hub = make_hub_device("hub_Z")
        service.link("USB_Z", "hub_Z", [dev], [hub])

        res = service.unlink(serial="USB_Z", hub_id="hub_Z")
        assert res is True
        assert len(registry.list()) == 0

    def test_unlink_conflict_mismatched_pair_raises_error(
        self, service: DeviceLinkService
    ) -> None:
        """Попытка отвязать не связанную друг с другом пару вызывает LinkConflictError."""
        dev1 = make_usb_device("USB_1", usb_info="1-1")
        dev2 = make_usb_device("USB_2", usb_info="1-2")
        hub1 = make_hub_device("hub_1")
        hub2 = make_hub_device("hub_2")

        service.link("USB_1", "hub_1", [dev1, dev2], [hub1, hub2])
        service.link("USB_2", "hub_2", [dev1, dev2], [hub1, hub2])

        with pytest.raises(LinkConflictError, match="Несоответствие"):
            service.unlink(serial="USB_1", hub_id="hub_2")

    def test_unlink_nonexistent_returns_false(
        self, service: DeviceLinkService
    ) -> None:
        """Попытка отвязать несуществующую связь возвращает False (идемпотентно)."""
        assert service.unlink(serial="UNKNOWN_SERIAL") is False
        assert service.unlink(hub_id="unknown_hub") is False

    def test_unlink_no_arguments_raises_validation_error(
        self, service: DeviceLinkService
    ) -> None:
        """Вызов unlink без аргументов вызывает LinkValidationError."""
        with pytest.raises(LinkValidationError, match="необходимо указать"):
            service.unlink()

    def test_no_implicit_unlinking_when_device_disconnects(
        self, service: DeviceLinkService, registry: LinkRegistry
    ) -> None:
        """Отвязка ТОЛЬКО явная: при отключении устройства связь НЕ сбрасывается автоматически."""
        dev = make_usb_device("USB_STAY", usb_info="1-1")
        hub = make_hub_device("hub_stay")

        service.link("USB_STAY", "hub_stay", [dev], [hub])
        assert len(registry.list()) == 1

        # Устройства исчезли из списков
        pairs = service.get_linked_pairs(local_devices=[], hub_devices=[])
        assert len(pairs) == 1
        assert pairs[0].local_present is False
        assert pairs[0].hub_present is False

        # Но связь в реестре осталась нетронутой
        assert len(registry.list()) == 1
        assert registry.get_hub_id("USB_STAY") == "hub_stay"


# ---------------------------------------------------------------------------
# 7. Тесты наблюдения статусов (Status Observation)
# ---------------------------------------------------------------------------


class TestStatusObservation:
    """Проверка отображения существующих пар и состояния присутствия каждой стороны."""

    def test_status_both_ready_and_online(
        self, service: DeviceLinkService
    ) -> None:
        """Обе стороны присутствуют, USB готов, хаб онлайн -> is_operational=True."""
        dev = make_usb_device("USB_READY", state="device", usb_info="1-1")
        hub = make_hub_device("hub_online", online=True)

        service.link("USB_READY", "hub_online", [dev], [hub])

        pairs = service.get_linked_pairs([dev], [hub])
        assert len(pairs) == 1
        p = pairs[0]

        assert p.serial == "USB_READY"
        assert p.hub_id == "hub_online"
        assert p.local_present is True
        assert p.local_ready is True
        assert p.hub_present is True
        assert p.hub_online is True
        assert p.is_operational is True
        assert p.is_connected is True
        assert "Обе стороны подключены и готовы" in p.status_summary

    def test_status_usb_ready_hub_offline(
        self, service: DeviceLinkService
    ) -> None:
        """USB готов, но устройство хаба offline -> is_operational=False, hub_online=False."""
        dev = make_usb_device("USB_READY", state="device", usb_info="1-1")
        hub = make_hub_device("hub_off", online=False)

        service.link("USB_READY", "hub_off", [dev], [hub])

        pairs = service.get_linked_pairs([dev], [hub])
        assert len(pairs) == 1
        p = pairs[0]

        assert p.local_ready is True
        assert p.hub_present is True
        assert p.hub_online is False
        assert p.is_operational is False
        assert "DroidHub не в сети" in p.status_summary

    def test_status_usb_unauthorized_hub_online(
        self, service: DeviceLinkService
    ) -> None:
        """Устройство хаба онлайн, но USB-устройство не авторизовано."""
        dev_ready = make_usb_device("USB_AUTH_TEST", state="device", usb_info="1-1")
        hub = make_hub_device("hub_1", online=True)
        service.link("USB_AUTH_TEST", "hub_1", [dev_ready], [hub])

        # При наблюдении телефон перешел в состояние unauthorized
        dev_unauth = make_usb_device("USB_AUTH_TEST", state="unauthorized", usb_info="1-1")

        pairs = service.get_linked_pairs([dev_unauth], [hub])
        assert len(pairs) == 1
        p = pairs[0]

        assert p.local_present is True
        assert p.local_ready is False
        assert p.hub_online is True
        assert p.is_operational is False
        assert "локальное USB-устройство не готово" in p.status_summary

    def test_status_usb_disconnected(self, service: DeviceLinkService) -> None:
        """USB отключено (отсутствует в local_devices), хаб онлайн."""
        dev = make_usb_device("USB_DISC", state="device", usb_info="1-1")
        hub = make_hub_device("hub_1", online=True)
        service.link("USB_DISC", "hub_1", [dev], [hub])

        pairs = service.get_linked_pairs(local_devices=[], hub_devices=[hub])
        assert len(pairs) == 1
        p = pairs[0]

        assert p.local_present is False
        assert p.local_ready is False
        assert p.hub_present is True
        assert p.hub_online is True
        assert p.is_operational is False
        assert "локальное USB-устройство отключено" in p.status_summary

    def test_status_hub_device_missing_from_list(
        self, service: DeviceLinkService
    ) -> None:
        """USB подключено, устройство хаба отсутствует в hub_devices."""
        dev = make_usb_device("USB_ALONE", state="device", usb_info="1-1")
        hub = make_hub_device("hub_missing", online=True)
        service.link("USB_ALONE", "hub_missing", [dev], [hub])

        pairs = service.get_linked_pairs(local_devices=[dev], hub_devices=[])
        assert len(pairs) == 1
        p = pairs[0]

        assert p.local_present is True
        assert p.local_ready is True
        assert p.hub_present is False
        assert p.hub_online is False
        assert "DroidHub отсутствует в списке" in p.status_summary

    def test_status_both_disconnected(
        self, service: DeviceLinkService
    ) -> None:
        """Оба устройства отсутствуют в переданных списках."""
        dev = make_usb_device("USB_GHOST", state="device", usb_info="1-1")
        hub = make_hub_device("hub_ghost", online=True)
        service.link("USB_GHOST", "hub_ghost", [dev], [hub])

        pairs = service.get_linked_pairs(local_devices=[], hub_devices=[])
        assert len(pairs) == 1
        p = pairs[0]

        assert p.local_present is False
        assert p.hub_present is False
        assert p.is_operational is False
        assert "Оба устройства отключены" in p.status_summary

    def test_get_single_pair_status(
        self, service: DeviceLinkService
    ) -> None:
        """Получение статуса конкретной пары по serial или hub_id."""
        dev = make_usb_device("DEV_SPECIFIC", state="device", usb_info="1-1")
        hub = make_hub_device("hub_specific", online=True)
        service.link("DEV_SPECIFIC", "hub_specific", [dev], [hub])

        by_s = service.get_pair_status(serial="DEV_SPECIFIC", local_devices=[dev], hub_devices=[hub])
        assert by_s is not None
        assert by_s.serial == "DEV_SPECIFIC"

        by_h = service.get_pair_status(hub_id="hub_specific", local_devices=[dev], hub_devices=[hub])
        assert by_h is not None
        assert by_h.hub_id == "hub_specific"

        by_unknown = service.get_pair_status(serial="UNKNOWN", local_devices=[dev], hub_devices=[hub])
        assert by_unknown is None

    def test_pair_status_to_dict_and_link_property(
        self, service: DeviceLinkService
    ) -> None:
        """LinkedPairStatus предоставляет доступ к to_dict() и свойству link."""
        dev = make_usb_device("DEV_TO_DICT", state="device", usb_info="1-1")
        hub = make_hub_device("hub_to_dict", online=True)
        service.link("DEV_TO_DICT", "hub_to_dict", [dev], [hub])

        p = service.get_linked_pairs([dev], [hub])[0]
        data = p.to_dict()

        assert data["serial"] == "DEV_TO_DICT"
        assert data["hub_id"] == "hub_to_dict"
        assert data["local_present"] is True
        assert data["hub_online"] is True
        assert data["is_operational"] is True
        assert isinstance(p.link, DeviceLink)
        assert p.link.serial == "DEV_TO_DICT"

    def test_get_available_devices_filters_already_linked(
        self, service: DeviceLinkService
    ) -> None:
        """get_available_usb_devices и get_available_hub_devices фильтруют уже привязанные устройства."""
        dev1 = make_usb_device("DEV_LINKED", usb_info="1-1")
        dev2 = make_usb_device("DEV_FREE", usb_info="1-2")
        dev_bad = make_usb_device("DEV_NET", is_network=True, usb_info="1-3")

        hub1 = make_hub_device("hub_linked")
        hub2 = make_hub_device("hub_free")

        service.link("DEV_LINKED", "hub_linked", [dev1], [hub1])

        avail_usb = service.get_available_usb_devices([dev1, dev2, dev_bad])
        assert [d.serial for d in avail_usb] == ["DEV_FREE"]

        avail_hub = service.get_available_hub_devices([hub1, hub2])
        assert [h.id for h in avail_hub] == ["hub_free"]


# ---------------------------------------------------------------------------
# 8. Тесты изоляции и отсутствия сетевых/системных вызовов
# ---------------------------------------------------------------------------


class TestIsolationAndSecurity:
    """Гарантия строго изолированной работы без сети, adb-команд и учетных данных."""

    def test_no_subprocess_calls_during_any_operation(
        self, service: DeviceLinkService
    ) -> None:
        """Никакие вызовы subprocess не происходят во время работы сервиса."""
        dev = make_usb_device("DEV_SEC", usb_info="1-1")
        hub = make_hub_device("hub_sec", online=True)

        with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
            # Привязка
            service.link("DEV_SEC", "hub_sec", [dev], [hub])
            # Наблюдение статусов
            service.get_linked_pairs([dev], [hub])
            # Отвязка
            service.unlink(serial="DEV_SEC")

            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    def test_no_socket_calls_during_operations(
        self, service: DeviceLinkService
    ) -> None:
        """Никакие вызовы socket не происходят во время работы сервиса."""
        dev = make_usb_device("DEV_SOCKET", usb_info="1-1")
        hub = make_hub_device("hub_socket", online=True)

        with patch("socket.socket") as mock_sock:
            service.link("DEV_SOCKET", "hub_socket", [dev], [hub])
            service.get_linked_pairs([dev], [hub])
            service.unlink(hub_id="hub_socket")

            mock_sock.assert_not_called()

    def test_reject_urls_and_tokens_in_identifiers(
        self, service: DeviceLinkService
    ) -> None:
        """Попытка передать сетевой URL или токен авторизации в качестве serial или hub_id блокируется."""
        dev = make_usb_device("http://example.com", usb_info="1-1")
        hub = make_hub_device("hub_1")

        with pytest.raises(DeviceLinkValidationError, match="не может быть сетевым URL"):
            service.link("http://example.com", "hub_1", [dev], [hub])

        with pytest.raises(DeviceLinkValidationError, match="не может содержать учетные данные"):
            service.link("VALID_SERIAL", "bearer token123", [dev], [hub])

    def test_validation_error_when_missing_context(self) -> None:
        """Сервис без переданных реестра или списков устройств сообщает об этом понятной ошибкой."""
        srv_bare = DeviceLinkService()

        with pytest.raises(LinkValidationError, match="LinkRegistry не предоставлен"):
            srv_bare.link("A", "B", [], [])

        with pytest.raises(LinkValidationError, match="local_devices не предоставлен"):
            registry = MagicMock(spec=LinkRegistry)
            srv_bare.link("A", "B", registry=registry)
