"""Тесты для изолированного клиентского компонента desktop/usb_presence.py.

Проверяют:
1. Готовность (Readiness): фильтрация по аппаратному USB, состоянию device,
   отсеивание сетевых, неавторизованных, оффлайн и непривязанных устройств.
2. Неоднозначность (Ambiguity): отбрасывание любых серийных номеров с повторами
   в локальном ADB-снимке.
3. Отключение и повторное подключение (Disconnect & Reconnect): исчезновение
   устройства из снимка при отключении, сохранение реестра на диске без изменений,
   восстановление при повторном подключении.
4. Независимость разных реестров: изоляция снимков для разных серверов/реестров.
5. Отличие ошибок реестра от валидного пустого снимка: поврежденный реестр
   выбрасывает исключение, а валидный пустой реестр возвращает пустой кортеж.
6. Стабильность и неизменяемость: детерминированная сортировка hub_id, тип tuple.
7. Чистота функции: отсутствие обращений к сети, подпроцессам ADB, GUI или записей на диск.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from desktop.adb_client import DeviceInfo
from desktop.device_links import (
    DeviceLink,
    DeviceLinkCorruptedFileError,
    DeviceLinkError,
    LinkRegistry,
)
from desktop.usb_presence import ready_linked_hub_ids


# ---------------------------------------------------------------------------
# Вспомогательные генераторы тестовых данных
# ---------------------------------------------------------------------------


def make_dev(
    serial: str = "USB_001",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    is_ready: bool | None = None,
    model: str | None = "Pixel 7a",
) -> DeviceInfo:
    """Создает тестовый объект DeviceInfo с заданными параметрами."""
    norm_state = state.lower().strip()
    ready = (norm_state == "device") if is_ready is None else is_ready
    conn_type = "Сетевое (ADB TCP/IP)" if is_network else "USB"
    return DeviceInfo(
        serial=serial,
        state=state,
        state_display="Готов к работе" if ready else f"Статус: {state}",
        is_network=is_network,
        connection_type=conn_type,
        is_ready=ready,
        model=model,
        usb_info=usb_info,
    )


@pytest.fixture
def registry(tmp_path: Path) -> LinkRegistry:
    """Фикстура чистого реестра связей во временном файле."""
    storage_path = tmp_path / "links.json"
    return LinkRegistry(storage_path)


# ---------------------------------------------------------------------------
# 1. Тесты на готовность (Readiness)
# ---------------------------------------------------------------------------


class TestReadiness:
    """Проверка критериев готовности аппаратного USB и состояния устройства."""

    def test_valid_usb_device_included(self, registry: LinkRegistry) -> None:
        """Готовое локальное USB-устройство, привязанное в реестре, попадает в снимок."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="device", is_network=False, usb_info="1-1.2")

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ("hub_phone_01",)

    def test_unauthorized_device_excluded(self, registry: LinkRegistry) -> None:
        """Неавторизованное устройство (state='unauthorized') отбрасывается."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="unauthorized", is_ready=False)

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_offline_device_excluded(self, registry: LinkRegistry) -> None:
        """Оффлайн-устройство (state='offline') отбрасывается."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="offline", is_ready=False)

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_network_flag_true_excluded(self, registry: LinkRegistry) -> None:
        """Сетевое ADB-устройство (is_network=True) отбрасывается."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="device", is_network=True)

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_network_serial_ip_port_excluded(self, registry: LinkRegistry) -> None:
        """Устройство с серийным номером вида IP:порт отбрасывается как сетевое."""
        net_serial = "192.168.1.100:5555"
        registry.link(net_serial, "hub_phone_01")
        dev = make_dev(net_serial, state="device", is_network=False, usb_info="1-1.2")

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_missing_usb_info_none_excluded(self, registry: LinkRegistry) -> None:
        """Устройство без usb_info (None) отбрасывается."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="device", usb_info=None)

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_empty_usb_info_string_excluded(self, registry: LinkRegistry) -> None:
        """Устройство с пустой строкой usb_info отбрасывается."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="device", usb_info="   ")

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_is_ready_false_excluded(self, registry: LinkRegistry) -> None:
        """Устройство со свойством is_ready=False при state='device' отбрасывается."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="device", is_ready=False)

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ()

    def test_unlinked_device_excluded(self, registry: LinkRegistry) -> None:
        """Локальное устройство, отсутствующее в реестре, не включается в снимок."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev_unlinked = make_dev("USB_UNLINKED", state="device", is_network=False)

        snapshot = ready_linked_hub_ids(registry, [dev_unlinked])
        assert snapshot == ()

    def test_case_insensitive_device_state(self, registry: LinkRegistry) -> None:
        """Состояние 'DEVICE' регистронезависимо считается готовым."""
        registry.link("USB_DEV_1", "hub_phone_01")
        dev = make_dev("USB_DEV_1", state="DEVICE", is_ready=True)

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert snapshot == ("hub_phone_01",)

    def test_multiple_devices_filtering(self, registry: LinkRegistry) -> None:
        """Смешанный список: в снимок попадают только полностью готовые и привязанные."""
        registry.link("USB_READY_1", "hub_phone_01")
        registry.link("USB_READY_2", "hub_phone_02")
        registry.link("USB_UNAUTH", "hub_phone_03")
        registry.link("USB_NET", "hub_phone_04")

        devices = [
            make_dev("USB_READY_1", state="device"),
            make_dev("USB_UNAUTH", state="unauthorized"),
            make_dev("USB_UNKNOWN", state="device"),  # непривязанное
            make_dev("USB_NET", state="device", is_network=True),
            make_dev("USB_READY_2", state="device"),
        ]

        snapshot = ready_linked_hub_ids(registry, devices)
        assert snapshot == ("hub_phone_01", "hub_phone_02")


# ---------------------------------------------------------------------------
# 2. Тесты на неоднозначность (Ambiguity)
# ---------------------------------------------------------------------------


class TestAmbiguity:
    """Проверка отбрасывания любой неоднозначности серийных номеров в ADB-снимке."""

    def test_duplicate_serial_both_valid_excluded(self, registry: LinkRegistry) -> None:
        """Дубликат одного serial в ADB-снимке (оба device) приводит к исключению записи."""
        registry.link("USB_DUP", "hub_phone_01")
        dev_a = make_dev("USB_DUP", state="device", usb_info="1-1.1")
        dev_b = make_dev("USB_DUP", state="device", usb_info="1-1.2")

        snapshot = ready_linked_hub_ids(registry, [dev_a, dev_b])
        assert snapshot == ()

    def test_duplicate_serial_mixed_states_excluded(self, registry: LinkRegistry) -> None:
        """Дубликат serial с разными состояниями (device + unauthorized) исключается."""
        registry.link("USB_DUP", "hub_phone_01")
        dev_a = make_dev("USB_DUP", state="device", is_ready=True)
        dev_b = make_dev("USB_DUP", state="unauthorized", is_ready=False)

        snapshot = ready_linked_hub_ids(registry, [dev_a, dev_b])
        assert snapshot == ()

    def test_duplicate_serial_usb_and_network_excluded(self, registry: LinkRegistry) -> None:
        """Дубликат serial с разным типом подключения (USB + network) исключается."""
        registry.link("USB_DUP", "hub_phone_01")
        dev_a = make_dev("USB_DUP", is_network=False)
        dev_b = make_dev("USB_DUP", is_network=True)

        snapshot = ready_linked_hub_ids(registry, [dev_a, dev_b])
        assert snapshot == ()

    def test_ambiguity_only_affects_conflicted_serial(self, registry: LinkRegistry) -> None:
        """Неоднозначность одного серийного номера не влияет на другие валидные устройства."""
        registry.link("USB_GOOD", "hub_phone_good")
        registry.link("USB_AMBIGUOUS", "hub_phone_ambiguous")

        devices = [
            make_dev("USB_AMBIGUOUS", state="device"),
            make_dev("USB_GOOD", state="device"),
            make_dev("USB_AMBIGUOUS", state="device"),
        ]

        snapshot = ready_linked_hub_ids(registry, devices)
        assert snapshot == ("hub_phone_good",)

    def test_ambiguity_in_unlinked_device_does_not_affect_linked(
        self, registry: LinkRegistry
    ) -> None:
        """Дубликаты в непривязанных устройствах не ломают снимок для привязанных."""
        registry.link("USB_LINKED", "hub_phone_01")

        devices = [
            make_dev("USB_UNLINKED", state="device"),
            make_dev("USB_LINKED", state="device"),
            make_dev("USB_UNLINKED", state="device"),
        ]

        snapshot = ready_linked_hub_ids(registry, devices)
        assert snapshot == ("hub_phone_01",)


# ---------------------------------------------------------------------------
# 3. Тесты на отключение и повторное подключение (Disconnect & Reconnect)
# ---------------------------------------------------------------------------


class TestDisconnectAndReconnect:
    """Проверка исчезновения из снимка при отключении USB и сохранности реестра."""

    def test_disconnect_removes_from_snapshot_registry_preserved(
        self, registry: LinkRegistry
    ) -> None:
        """Пропажа USB убирает запись из снимка, а реестр на диске остается неизменным."""
        registry.link("USB_PHONE_1", "hub_phone_01")
        dev = make_dev("USB_PHONE_1", state="device")

        # 1. Устройство подключено: присутствует в снимке
        assert ready_linked_hub_ids(registry, [dev]) == ("hub_phone_01",)

        # 2. Кабель отключен: список devices пуст, снимок пуст
        assert ready_linked_hub_ids(registry, []) == ()

        # 3. Реестр на диске НЕ был изменен: привязка сохранена
        assert len(registry.list()) == 1
        assert registry.get_hub_id("USB_PHONE_1") == "hub_phone_01"

        # Чтение файла с диска напрямую подтверждает сохранность
        raw_reg = LinkRegistry(registry.path)
        assert len(raw_reg.list()) == 1
        assert raw_reg.get_hub_id("USB_PHONE_1") == "hub_phone_01"

        # 4. Кабель снова подключен: снимок снова возвращает ID хаба
        assert ready_linked_hub_ids(registry, [dev]) == ("hub_phone_01",)

    def test_partial_disconnect_in_multi_device(self, registry: LinkRegistry) -> None:
        """Отключение одного из нескольких устройств оставляет остальные в снимке."""
        registry.link("USB_1", "hub_01")
        registry.link("USB_2", "hub_02")

        dev1 = make_dev("USB_1")
        dev2 = make_dev("USB_2")

        # Оба подключены
        assert ready_linked_hub_ids(registry, [dev1, dev2]) == ("hub_01", "hub_02")

        # Отключили dev1
        assert ready_linked_hub_ids(registry, [dev2]) == ("hub_02",)

        # Вернули dev1
        assert ready_linked_hub_ids(registry, [dev1, dev2]) == ("hub_01", "hub_02")

        # Реестр остался неизменным
        assert len(registry.list()) == 2


# ---------------------------------------------------------------------------
# 4. Тесты на независимость разных реестров (Registry Independence)
# ---------------------------------------------------------------------------


class TestRegistryIndependence:
    """Проверка независимости снимков для разных серверов / файлов реестров."""

    def test_different_server_registries_same_devices(self, tmp_path: Path) -> None:
        """Один и тот же локальный телефон выдает разные ID хаба для разных серверов."""
        reg_server_a = LinkRegistry(tmp_path / "server_a.json")
        reg_server_b = LinkRegistry(tmp_path / "server_b.json")

        reg_server_a.link("USB_001", "hub_alpha_01")
        reg_server_b.link("USB_001", "hub_beta_01")

        devices = [make_dev("USB_001")]

        snapshot_a = ready_linked_hub_ids(reg_server_a, devices)
        snapshot_b = ready_linked_hub_ids(reg_server_b, devices)

        assert snapshot_a == ("hub_alpha_01",)
        assert snapshot_b == ("hub_beta_01",)

    def test_one_server_linked_another_unlinked(self, tmp_path: Path) -> None:
        """Устройство привязано на сервере А, но не привязано на сервере Б."""
        reg_server_a = LinkRegistry(tmp_path / "server_a.json")
        reg_server_b = LinkRegistry(tmp_path / "server_b.json")

        reg_server_a.link("USB_001", "hub_alpha_01")
        # Сервер Б не имеет связей

        devices = [make_dev("USB_001")]

        assert ready_linked_hub_ids(reg_server_a, devices) == ("hub_alpha_01",)
        assert ready_linked_hub_ids(reg_server_b, devices) == ()


# ---------------------------------------------------------------------------
# 5. Тесты на отличие ошибок реестра от валидного пустого снимка
# ---------------------------------------------------------------------------


class TestRegistryErrorsVsValidEmptySnapshot:
    """Проверка различимости сбоя реестра от нормального пустого результата."""

    def test_valid_empty_snapshot_nonexistent_file(self, tmp_path: Path) -> None:
        """Отсутствие файла реестра — это валидный пустой реестр, возвращающий ()."""
        reg = LinkRegistry(tmp_path / "nonexistent.json")
        assert ready_linked_hub_ids(reg, []) == ()
        assert ready_linked_hub_ids(reg, [make_dev("USB_001")]) == ()

    def test_valid_empty_snapshot_empty_links(self, tmp_path: Path) -> None:
        """Файл с пустой структурой связей возвращает валидный пустой кортеж ()."""
        reg_file = tmp_path / "empty.json"
        reg_file.write_text('{"version": 1, "links": []}', encoding="utf-8")
        reg = LinkRegistry(reg_file)

        assert ready_linked_hub_ids(reg, []) == ()
        assert ready_linked_hub_ids(reg, [make_dev("USB_001")]) == ()

    def test_corrupted_file_syntax_error_raises_error(self, tmp_path: Path) -> None:
        """Битая JSON-структура файла реестра выбрасывает исключение, а не пустой кортеж."""
        corrupt_file = tmp_path / "corrupt.json"
        corrupt_file.write_text("{broken json syntax", encoding="utf-8")
        reg = LinkRegistry(corrupt_file)

        # Даже при пустом списке devices ошибка реестра обязана всплывать
        with pytest.raises(DeviceLinkCorruptedFileError):
            ready_linked_hub_ids(reg, [])

        with pytest.raises(DeviceLinkCorruptedFileError):
            ready_linked_hub_ids(reg, [make_dev("USB_001")])

    @pytest.mark.parametrize(
        "broken_content",
        [
            "",  # пустой файл
            "[]",  # корневой массив вместо объекта
            '{"links": []}',  # нет поля version
            '{"version": 999, "links": []}',  # неподдерживаемая версия
            '{"version": 1, "links": [{"serial": "s1"}]}',  # нет hub_id
            '{"version": 1, "links": [{"serial": "s1", "hub_id": "h1"}, {"serial": "s1", "hub_id": "h2"}]}',  # дубликат serial
            '{"version": 1, "links": [{"serial": "s1", "hub_id": "h1"}, {"serial": "s2", "hub_id": "h1"}]}',  # дубликат hub_id
        ],
    )
    def test_invalid_schema_contents_raise_error(
        self, tmp_path: Path, broken_content: str
    ) -> None:
        """Любые нарушения схемы реестра выбрасывают исключение целостности."""
        corrupt_file = tmp_path / "bad_schema.json"
        corrupt_file.write_text(broken_content, encoding="utf-8")
        reg = LinkRegistry(corrupt_file)

        with pytest.raises(DeviceLinkCorruptedFileError):
            ready_linked_hub_ids(reg, [])

    def test_registry_marked_corrupted_raises_error(self, registry: LinkRegistry) -> None:
        """Реестр с установленным флагом повреждения выбрасывает исключение."""
        registry._corrupted = True

        with pytest.raises(DeviceLinkCorruptedFileError):
            ready_linked_hub_ids(registry, [])

    def test_none_arguments_raise_type_error(self, registry: LinkRegistry) -> None:
        """Передача None вместо реестра или devices выбрасывает TypeError."""
        with pytest.raises(TypeError, match="registry is required"):
            ready_linked_hub_ids(None, [])  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="devices is required"):
            ready_linked_hub_ids(registry, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 6. Тесты на стабильность и сортировку (Stability & Sorting)
# ---------------------------------------------------------------------------


class TestStabilityAndSorting:
    """Проверка гарантированной сортировки и неизменяемости снимка."""

    def test_snapshot_is_sorted_alphabetically(self, registry: LinkRegistry) -> None:
        """Снимок детерминированно отсортирован по hub_id независимо от порядка ввода."""
        registry.link("USB_Z", "hub_zebra")
        registry.link("USB_A", "hub_alpha")
        registry.link("USB_M", "hub_monkey")

        # Передаем в обратном порядке
        devices = [
            make_dev("USB_Z"),
            make_dev("USB_M"),
            make_dev("USB_A"),
        ]

        snapshot = ready_linked_hub_ids(registry, devices)
        assert snapshot == ("hub_alpha", "hub_monkey", "hub_zebra")

    def test_snapshot_returns_immutable_tuple(self, registry: LinkRegistry) -> None:
        """Возвращаемый снимок является кортежем (tuple)."""
        registry.link("USB_1", "hub_01")
        dev = make_dev("USB_1")

        snapshot = ready_linked_hub_ids(registry, [dev])
        assert isinstance(snapshot, tuple)
        assert type(snapshot) is tuple

        empty_snapshot = ready_linked_hub_ids(registry, [])
        assert isinstance(empty_snapshot, tuple)
        assert empty_snapshot == ()


# ---------------------------------------------------------------------------
# 7. Тесты на чистоту функции (No Side Effects)
# ---------------------------------------------------------------------------


class TestPureFunctionGuarantees:
    """Проверка отсутствия побочных эффектов: сети, подпроцессов ADB, GUI и записей на диск."""

    def test_no_subprocesses_or_adb_commands(self, registry: LinkRegistry) -> None:
        """ready_linked_hub_ids не выполняет никаких команд ОС или ADB-вызовов."""
        registry.link("USB_001", "hub_001")
        dev = make_dev("USB_001")

        with patch("subprocess.Popen") as mock_popen, \
             patch("subprocess.run") as mock_run, \
             patch("subprocess.call") as mock_call, \
             patch("subprocess.check_output") as mock_check_output:
            snapshot = ready_linked_hub_ids(registry, [dev])
            assert snapshot == ("hub_001",)
            mock_popen.assert_not_called()
            mock_run.assert_not_called()
            mock_call.assert_not_called()
            mock_check_output.assert_not_called()

    def test_no_network_connections(self, registry: LinkRegistry) -> None:
        """ready_linked_hub_ids не инициирует сетевых соединений (сокетов/HTTP)."""
        registry.link("USB_001", "hub_001")
        dev = make_dev("USB_001")

        with patch("socket.socket.connect") as mock_socket_connect, \
             patch("urllib.request.urlopen") as mock_urlopen:
            snapshot = ready_linked_hub_ids(registry, [dev])
            assert snapshot == ("hub_001",)
            mock_socket_connect.assert_not_called()
            mock_urlopen.assert_not_called()

    def test_no_disk_writes_occur(self, registry: LinkRegistry) -> None:
        """ready_linked_hub_ids не выполняет файловых изменений на диске."""
        registry.link("USB_001", "hub_001")
        dev = make_dev("USB_001")

        initial_content = registry.path.read_text(encoding="utf-8")
        initial_mtime = registry.path.stat().st_mtime_ns

        # Вызываем для подключенного устройства
        ready_linked_hub_ids(registry, [dev])

        # Вызываем для отключенного устройства
        ready_linked_hub_ids(registry, [])

        after_content = registry.path.read_text(encoding="utf-8")
        after_mtime = registry.path.stat().st_mtime_ns

        assert initial_content == after_content
        assert initial_mtime == after_mtime
