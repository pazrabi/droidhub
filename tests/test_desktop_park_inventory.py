"""
Unit-тесты для desktop/park_inventory.py.

Проверяет:
- контракт ParkDevice (immutable dataclass, key usb:/hub:, routes, availability)
- объединение USB и Hub устройств (1:1 явные привязки)
- коллизии привязок (две Hub-цели на один serial)
- legacy объекты и mappings
- offline и stale снимки
- только Hub / только USB
- обе пустые выборки
- сетевые устройства ADB (host:port)
- значения заряда батареи (0, False, None, проценты)
- политику resolve_hub_startup_url
- дедупликацию и детерминированный порядок без мутации входов
"""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import unittest

from desktop.park_inventory import (
    ParkDevice,
    build_park_inventory,
    resolve_hub_startup_url,
)


class LegacyUsbDevice:
    """Эмуляция существующего/legacy объекта DeviceInfo из adb_client."""

    def __init__(
        self,
        serial: str,
        state: str = "device",
        is_ready: bool = True,
        model: str | None = None,
        model_display: str | None = None,
        name: str | None = None,
        is_network: bool = False,
        connection_type: str = "USB",
    ) -> None:
        self.serial = serial
        self.state = state
        self.is_ready = is_ready
        self.model = model
        self.model_display = model_display
        self.name = name
        self.is_network = is_network
        self.connection_type = connection_type


class LegacyHubDevice:
    """Эмуляция существующего/legacy объекта HubDevice из hub_client."""

    def __init__(
        self,
        id: str,
        name: str = "",
        online: bool = True,
        battery: int | None = None,
        model: str | None = None,
    ) -> None:
        self.id = id
        self.name = name or id
        self.online = online
        self.battery = battery
        self.model = model


class TestParkDeviceContract(unittest.TestCase):
    """Проверка контракта immutable dataclass ParkDevice."""

    def test_immutability(self) -> None:
        dev = ParkDevice(
            key="usb:ABC12345",
            name="Test Phone",
            model="Pixel 7",
            usb_serial="ABC12345",
            hub_id=None,
            usb_ready=True,
            hub_online=False,
            hub_stale=False,
            battery=80,
        )
        with self.assertRaises(FrozenInstanceError):
            dev.name = "New Name"  # type: ignore[misc]

        with self.assertRaises(FrozenInstanceError):
            dev.usb_ready = False  # type: ignore[misc]

    def test_routes_property_order(self) -> None:
        # Только USB
        usb_only = ParkDevice(key="usb:dev1", usb_serial="dev1")
        self.assertEqual(usb_only.routes, ("USB",))

        # Только Hub
        hub_only = ParkDevice(key="hub:h1", hub_id="h1")
        self.assertEqual(hub_only.routes, ("Hub",))

        # Объединенное устройство: порядок строго ("USB", "Hub")
        merged = ParkDevice(key="usb:dev1", usb_serial="dev1", hub_id="h1")
        self.assertEqual(merged.routes, ("USB", "Hub"))

        # Пустые идентификаторы
        empty_dev = ParkDevice(key="usb:none")
        self.assertEqual(empty_dev.routes, ())

    def test_no_fake_video_field(self) -> None:
        dev = ParkDevice(key="hub:remote-1", hub_id="remote-1")
        self.assertFalse(hasattr(dev, "video_stream"))
        self.assertFalse(hasattr(dev, "has_video"))


class TestEmptyInputs(unittest.TestCase):
    """Проверка граничных условий с пустыми входами."""

    def test_both_empty_lists(self) -> None:
        res = build_park_inventory([], [])
        self.assertEqual(res, [])

    def test_both_none(self) -> None:
        res = build_park_inventory(None, None)
        self.assertEqual(res, [])

    def test_empty_tuples_and_generators(self) -> None:
        def empty_gen():
            if False:
                yield {}

        res = build_park_inventory(empty_gen(), (), hub_to_usb={})
        self.assertEqual(res, [])


class TestUsbOnlyAndDeduplication(unittest.TestCase):
    """Проверка обработки только локальных USB-устройств."""

    def test_usb_mapping_ready_and_unauthorized(self) -> None:
        usb_rows = [
            {
                "serial": "device-1",
                "model": "Pixel_6",
                "model_display": "Pixel 6",
                "name": "Рабочий Pixel",
                "state": "device",
                "is_ready": True,
            },
            {
                "serial": "device-2",
                "model": "Galaxy_S21",
                "model_display": "Galaxy S21",
                "state": "unauthorized",
                "is_ready": False,
            },
        ]
        inventory = build_park_inventory(usb_rows, [])
        self.assertEqual(len(inventory), 2)

        d1 = inventory[0]
        self.assertEqual(d1.key, "usb:device-1")
        self.assertEqual(d1.name, "Рабочий Pixel")
        self.assertEqual(d1.model, "Pixel 6")
        self.assertEqual(d1.usb_serial, "device-1")
        self.assertIsNone(d1.hub_id)
        self.assertTrue(d1.usb_ready)
        self.assertFalse(d1.hub_online)
        self.assertFalse(d1.hub_stale)
        self.assertIsNone(d1.battery)
        self.assertEqual(d1.routes, ("USB",))
        self.assertTrue(d1.is_available)

        d2 = inventory[1]
        self.assertEqual(d2.key, "usb:device-2")
        self.assertEqual(d2.model, "Galaxy S21")
        self.assertFalse(d2.usb_ready)
        self.assertEqual(d2.routes, ("USB",))
        self.assertFalse(d2.is_available)

    def test_deduplication_of_repeat_serials(self) -> None:
        usb_rows = [
            {"serial": "s1", "model": "Model-A", "state": "device"},
            {"serial": "s2", "model": "Model-B", "state": "device"},
            {"serial": "s1", "model": "Model-A-Duplicate", "state": "offline"},
        ]
        inventory = build_park_inventory(usb_rows, [])
        self.assertEqual(len(inventory), 2)
        self.assertEqual(inventory[0].usb_serial, "s1")
        self.assertEqual(inventory[0].model, "Model-A")
        self.assertTrue(inventory[0].usb_ready)
        self.assertEqual(inventory[1].usb_serial, "s2")

    def test_usb_snapshot_not_mutated(self) -> None:
        usb_rows = [
            {"serial": "s1", "model": "Pixel 6", "state": "device", "is_ready": True},
            {"serial": "s2", "model": "Galaxy", "state": "unauthorized", "is_ready": False},
        ]
        snapshot_copy = copy.deepcopy(usb_rows)
        _ = build_park_inventory(usb_rows, [], hub_to_usb={"h1": "s1"})
        self.assertEqual(usb_rows, snapshot_copy)


class TestHubOnlyAndStale(unittest.TestCase):
    """Проверка Hub-only устройств, сетевых сбоев и устаревания."""

    def test_hub_mapping_online_and_offline(self) -> None:
        hub_rows = [
            {"id": "hub-phone-1", "name": "Hub Device 1", "online": True, "battery": 90},
            {"id": "hub-phone-2", "name": "Hub Device 2", "online": False, "battery": 15},
        ]
        inventory = build_park_inventory([], hub_rows, hub_stale=False)
        self.assertEqual(len(inventory), 2)

        h1 = inventory[0]
        self.assertEqual(h1.key, "hub:hub-phone-1")
        self.assertEqual(h1.name, "Hub Device 1")
        self.assertIsNone(h1.usb_serial)
        self.assertEqual(h1.hub_id, "hub-phone-1")
        self.assertFalse(h1.usb_ready)
        self.assertTrue(h1.hub_online)
        self.assertFalse(h1.hub_stale)
        self.assertEqual(h1.battery, 90)
        self.assertEqual(h1.routes, ("Hub",))
        self.assertTrue(h1.is_available)

        h2 = inventory[1]
        self.assertEqual(h2.key, "hub:hub-phone-2")
        self.assertFalse(h2.hub_online)
        self.assertFalse(h2.hub_stale)
        self.assertFalse(h2.is_available)

    def test_hub_stale_on_network_failure(self) -> None:
        # Hub-only сохраняет статус (online=True, battery=75), но hub_stale=True
        # и не выдается как доступный для операции (is_available=False).
        hub_rows = [
            {"id": "hub-phone-1", "name": "Stale Phone", "online": True, "battery": 75},
        ]
        inventory = build_park_inventory([], hub_rows, hub_stale=True)
        self.assertEqual(len(inventory), 1)

        dev = inventory[0]
        self.assertTrue(dev.hub_online)  # статус сохранен
        self.assertTrue(dev.hub_stale)   # снимок помечен как устаревший
        self.assertEqual(dev.battery, 75)
        self.assertEqual(dev.routes, ("Hub",))
        self.assertFalse(dev.is_available)  # не выдается для операций
        self.assertEqual(dev.available_routes, ())

    def test_stale_hub_does_not_block_usb(self) -> None:
        # Отсутствие/устаревание Hub не блокирует USB
        usb_rows = [{"serial": "usb-1", "model": "Pixel 7", "is_ready": True}]
        hub_rows = [{"id": "hub-1", "name": "Remote Pixel 7", "online": True, "battery": 80}]
        hub_to_usb = {"hub-1": "usb-1"}

        inventory = build_park_inventory(usb_rows, hub_rows, hub_to_usb=hub_to_usb, hub_stale=True)
        self.assertEqual(len(inventory), 1)

        dev = inventory[0]
        self.assertEqual(dev.key, "usb:usb-1")
        self.assertEqual(dev.usb_serial, "usb-1")
        self.assertEqual(dev.hub_id, "hub-1")
        self.assertTrue(dev.usb_ready)
        self.assertTrue(dev.hub_online)
        self.assertTrue(dev.hub_stale)
        self.assertEqual(dev.routes, ("USB", "Hub"))
        # USB-маршрут доступен, поэтому устройство в целом доступно для операций
        self.assertTrue(dev.is_available)
        self.assertEqual(dev.available_routes, ("USB",))

    def test_deduplication_of_repeat_hub_ids(self) -> None:
        hub_rows = [
            {"id": "h1", "name": "Name-A", "online": True},
            {"id": "h2", "name": "Name-B", "online": False},
            {"id": "h1", "name": "Name-A-Dup", "online": False},
        ]
        inventory = build_park_inventory([], hub_rows)
        self.assertEqual(len(inventory), 2)
        self.assertEqual(inventory[0].hub_id, "h1")
        self.assertEqual(inventory[0].name, "Name-A")
        self.assertTrue(inventory[0].hub_online)
        self.assertEqual(inventory[1].hub_id, "h2")


class TestBindingMapAndMerging(unittest.TestCase):
    """Проверка явных привязок 1:1 и порядка объединения."""

    def test_unambiguous_1to1_merge(self) -> None:
        usb_rows = [
            {"serial": "SN-001", "model": "Pixel_7", "model_display": "Pixel 7", "is_ready": True},
            {"serial": "SN-002", "model": "Galaxy_S22", "is_ready": True},
        ]
        hub_rows = [
            {"id": "remote-dev-1", "name": "Стендовый Pixel 7", "online": True, "battery": 85},
            {"id": "remote-dev-standalone", "name": "Удаленный телефон", "online": True, "battery": 45},
        ]
        hub_to_usb = {"remote-dev-1": "SN-001"}

        inventory = build_park_inventory(usb_rows, hub_rows, hub_to_usb=hub_to_usb)
        self.assertEqual(len(inventory), 3)

        # 1. Объединенный SN-001
        m = inventory[0]
        self.assertEqual(m.key, "usb:SN-001")
        self.assertEqual(m.name, "Стендовый Pixel 7")
        self.assertEqual(m.model, "Pixel 7")
        self.assertEqual(m.usb_serial, "SN-001")
        self.assertEqual(m.hub_id, "remote-dev-1")
        self.assertTrue(m.usb_ready)
        self.assertTrue(m.hub_online)
        self.assertFalse(m.hub_stale)
        self.assertEqual(m.battery, 85)
        self.assertEqual(m.routes, ("USB", "Hub"))

        # 2. USB-only SN-002
        u2 = inventory[1]
        self.assertEqual(u2.key, "usb:SN-002")
        self.assertIsNone(u2.hub_id)
        self.assertEqual(u2.routes, ("USB",))

        # 3. Hub-only remote-dev-standalone
        h_standalone = inventory[2]
        self.assertEqual(h_standalone.key, "hub:remote-dev-standalone")
        self.assertIsNone(h_standalone.usb_serial)
        self.assertEqual(h_standalone.routes, ("Hub",))

    def test_name_preference_in_merge(self) -> None:
        # Если в USB задано имя пользователя, оно имеет приоритет
        usb_rows = [{"serial": "s1", "name": "USB Custom Name", "model": "M1", "is_ready": True}]
        hub_rows = [{"id": "h1", "name": "Hub Rack Name", "online": True}]
        inventory = build_park_inventory(usb_rows, hub_rows, hub_to_usb={"h1": "s1"})
        self.assertEqual(inventory[0].name, "USB Custom Name")

        # Если в USB имя отсутствует, берется имя из Hub
        usb_no_name = [{"serial": "s2", "model": "M2", "is_ready": True}]
        hub_with_name = [{"id": "h2", "name": "Hub Rack Name 2", "online": True}]
        inventory2 = build_park_inventory(usb_no_name, hub_with_name, hub_to_usb={"h2": "s2"})
        self.assertEqual(inventory2[0].name, "Hub Rack Name 2")

    def test_hub_to_usb_unmatched_entries(self) -> None:
        # Привязка ссылается на serial, которого нет в usb_rows
        usb_rows = [{"serial": "s1", "model": "M1", "is_ready": True}]
        hub_rows = [{"id": "h1", "name": "H1", "online": True}]
        hub_to_usb = {"h1": "unknown-serial"}

        inventory = build_park_inventory(usb_rows, hub_rows, hub_to_usb=hub_to_usb)
        self.assertEqual(len(inventory), 2)
        self.assertEqual(inventory[0].key, "usb:s1")
        self.assertIsNone(inventory[0].hub_id)
        self.assertEqual(inventory[1].key, "hub:h1")
        self.assertIsNone(inventory[1].usb_serial)


class TestCollisions(unittest.TestCase):
    """Проверка обработки коллизий привязок."""

    def test_collision_two_hubs_to_one_serial(self) -> None:
        # При коллизии две Hub-цели привязаны к одному serial:
        # не выбирать произвольную цель, ни одно устройство не объединять!
        usb_rows = [{"serial": "serial-X", "model": "Pixel 6", "is_ready": True}]
        hub_rows = [
            {"id": "hub-target-1", "name": "Target 1", "online": True},
            {"id": "hub-target-2", "name": "Target 2", "online": True},
        ]
        hub_to_usb = {
            "hub-target-1": "serial-X",
            "hub-target-2": "serial-X",
        }

        inventory = build_park_inventory(usb_rows, hub_rows, hub_to_usb=hub_to_usb)
        # Все 3 остаются раздельно
        self.assertEqual(len(inventory), 3)

        usb_dev = inventory[0]
        self.assertEqual(usb_dev.key, "usb:serial-X")
        self.assertIsNone(usb_dev.hub_id)
        self.assertEqual(usb_dev.routes, ("USB",))

        h1 = inventory[1]
        self.assertEqual(h1.key, "hub:hub-target-1")
        self.assertIsNone(h1.usb_serial)
        self.assertEqual(h1.routes, ("Hub",))

        h2 = inventory[2]
        self.assertEqual(h2.key, "hub:hub-target-2")
        self.assertIsNone(h2.usb_serial)
        self.assertEqual(h2.routes, ("Hub",))

    def test_collision_does_not_affect_unrelated_devices(self) -> None:
        usb_rows = [
            {"serial": "serial-collided", "model": "Pixel 6", "is_ready": True},
            {"serial": "serial-clean", "model": "Pixel 7", "is_ready": True},
        ]
        hub_rows = [
            {"id": "h-collided-1", "name": "C1", "online": True},
            {"id": "h-collided-2", "name": "C2", "online": True},
            {"id": "h-clean", "name": "Clean Hub", "online": True, "battery": 70},
        ]
        hub_to_usb = {
            "h-collided-1": "serial-collided",
            "h-collided-2": "serial-collided",
            "h-clean": "serial-clean",
        }

        inventory = build_park_inventory(usb_rows, hub_rows, hub_to_usb=hub_to_usb)
        self.assertEqual(len(inventory), 4)

        # serial-collided остается только USB
        self.assertEqual(inventory[0].key, "usb:serial-collided")
        self.assertIsNone(inventory[0].hub_id)

        # serial-clean успешно объединяется с h-clean
        self.assertEqual(inventory[1].key, "usb:serial-clean")
        self.assertEqual(inventory[1].hub_id, "h-clean")
        self.assertEqual(inventory[1].routes, ("USB", "Hub"))

        # h-collided-1 и h-collided-2 остаются Hub-only
        self.assertEqual(inventory[2].key, "hub:h-collided-1")
        self.assertEqual(inventory[3].key, "hub:h-collided-2")


class TestLegacyObjects(unittest.TestCase):
    """Проверка совместимости с объектами классов (legacy DeviceInfo, HubDevice)."""

    def test_legacy_objects_support(self) -> None:
        usb_dev = LegacyUsbDevice(
            serial="legacy-usb-123",
            model="SM_G991B",
            model_display="Galaxy S21",
            state="device",
            is_ready=True,
            name="Мой Galaxy",
        )
        hub_dev = LegacyHubDevice(
            id="legacy-hub-999",
            name="Офисный стенд",
            online=True,
            battery=88,
        )

        inventory = build_park_inventory(
            [usb_dev],
            [hub_dev],
            hub_to_usb={"legacy-hub-999": "legacy-usb-123"},
        )
        self.assertEqual(len(inventory), 1)

        dev = inventory[0]
        self.assertEqual(dev.key, "usb:legacy-usb-123")
        self.assertEqual(dev.name, "Мой Galaxy")
        self.assertEqual(dev.model, "Galaxy S21")
        self.assertEqual(dev.usb_serial, "legacy-usb-123")
        self.assertEqual(dev.hub_id, "legacy-hub-999")
        self.assertTrue(dev.usb_ready)
        self.assertTrue(dev.hub_online)
        self.assertEqual(dev.battery, 88)
        self.assertEqual(dev.routes, ("USB", "Hub"))

    def test_legacy_objects_mixed_with_mappings(self) -> None:
        usb_obj = LegacyUsbDevice(serial="obj-usb", is_ready=True, model="Pixel")
        usb_dict = {"serial": "dict-usb", "is_ready": False, "model": "Nexus"}

        hub_obj = LegacyHubDevice(id="obj-hub", online=True, battery=60)
        hub_dict = {"id": "dict-hub", "online": False, "battery": 20}

        inventory = build_park_inventory([usb_obj, usb_dict], [hub_obj, hub_dict])
        self.assertEqual(len(inventory), 4)
        self.assertEqual(inventory[0].usb_serial, "obj-usb")
        self.assertEqual(inventory[1].usb_serial, "dict-usb")
        self.assertEqual(inventory[2].hub_id, "obj-hub")
        self.assertEqual(inventory[3].hub_id, "dict-hub")


class TestNetworkAdbDevices(unittest.TestCase):
    """Проверка поддержки сетевых устройств ADB (IP:port)."""

    def test_network_adb_serial(self) -> None:
        net_dev = {
            "serial": "192.168.1.150:5555",
            "model": "Pixel_5",
            "model_display": "Pixel 5",
            "is_network": True,
            "connection_type": "Сетевое (ADB TCP/IP)",
            "state": "device",
            "is_ready": True,
        }
        inventory = build_park_inventory([net_dev], [])
        self.assertEqual(len(inventory), 1)

        dev = inventory[0]
        self.assertEqual(dev.key, "usb:192.168.1.150:5555")
        self.assertEqual(dev.usb_serial, "192.168.1.150:5555")
        self.assertEqual(dev.model, "Pixel 5")
        self.assertTrue(dev.usb_ready)
        self.assertEqual(dev.routes, ("USB",))

    def test_network_adb_merge_with_hub(self) -> None:
        net_dev = {
            "serial": "10.0.0.42:5555",
            "model": "Tablet",
            "is_ready": True,
        }
        hub_row = {
            "id": "hub-tablet-1",
            "name": "Rack Tablet",
            "online": True,
            "battery": 100,
        }
        inventory = build_park_inventory(
            [net_dev],
            [hub_row],
            hub_to_usb={"hub-tablet-1": "10.0.0.42:5555"},
        )
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0].key, "usb:10.0.0.42:5555")
        self.assertEqual(inventory[0].hub_id, "hub-tablet-1")
        self.assertEqual(inventory[0].routes, ("USB", "Hub"))


class TestBatteryAndZeroValues(unittest.TestCase):
    """Проверка корректного сохранения значений 0, False и различных форматов батареи."""

    def test_battery_zero_and_false_preservation(self) -> None:
        hub_rows = [
            {"id": "h-zero", "online": False, "battery": 0},
            {"id": "h-false", "online": False, "battery": False},
            {"id": "h-str-zero", "online": True, "battery": "0%"},
            {"id": "h-str-pct", "online": True, "battery": "75%"},
            {"id": "h-none", "online": True, "battery": None},
        ]
        inventory = build_park_inventory([], hub_rows)
        self.assertEqual(len(inventory), 5)

        # 0 сохраняется как int 0, а не None
        self.assertIs(inventory[0].battery, 0)
        self.assertIs(inventory[0].hub_online, False)

        # False не превращается в 0, а становится None
        self.assertIsNone(inventory[1].battery)
        self.assertIs(inventory[1].hub_online, False)

        # "0%" парсится как 0
        self.assertEqual(inventory[2].battery, 0)

        # "75%" парсится как 75
        self.assertEqual(inventory[3].battery, 75)

        # None сохраняется как None
        self.assertIsNone(inventory[4].battery)


class TestResolveHubStartupUrl(unittest.TestCase):
    """Проверка чистой функции выбора URL автоподключения при старте."""

    def test_disabled_on_startup(self) -> None:
        # При выключении возвращает None независимо от сохраненного и дефолтного адреса
        res = resolve_hub_startup_url(
            "https://hub.custom.org",
            connect_on_startup=False,
            default_url="https://hub.product.org",
        )
        self.assertIsNone(res)

    def test_enabled_uses_saved_url(self) -> None:
        # При включении сохраненный адрес имеет приоритет
        res = resolve_hub_startup_url(
            "https://hub.saved.org",
            connect_on_startup=True,
            default_url="https://hub.default.org",
        )
        self.assertEqual(res, "https://hub.saved.org")

    def test_enabled_uses_default_when_saved_is_empty(self) -> None:
        # При включении и пустом сохраненном используется default_url продукта
        res = resolve_hub_startup_url(
            "",
            connect_on_startup=True,
            default_url="https://hub.default.org",
        )
        self.assertEqual(res, "https://hub.default.org")

        res_none_saved = resolve_hub_startup_url(
            None,
            connect_on_startup=True,
            default_url="https://hub.default.org",
        )
        self.assertEqual(res_none_saved, "https://hub.default.org")

    def test_enabled_both_empty_returns_none(self) -> None:
        # При пустых обоих возвращает None
        self.assertIsNone(
            resolve_hub_startup_url(
                "",
                connect_on_startup=True,
                default_url="",
            )
        )
        self.assertIsNone(
            resolve_hub_startup_url(
                "   ",
                connect_on_startup=True,
                default_url="   ",
            )
        )


if __name__ == "__main__":
    unittest.main()
