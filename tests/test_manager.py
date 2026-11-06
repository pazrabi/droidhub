from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
from types import ModuleType
import pytest

# Заглушка hub.config, если модуль еще не создан параллельной задачей
if "hub.config" not in sys.modules:
    try:
        import hub.config  # type: ignore
    except ImportError:
        _cfg_mod = ModuleType("hub.config")

        @dataclass
        class DeviceConfig:
            id: str
            name: str
            host: str
            port: int = 5555

        @dataclass
        class Settings:
            devices_file: str = "config/devices.yaml"
            db_path: str = "data/hub.db"
            admin_user: str = "admin"
            admin_password_hash: str = ""
            session_secret: str = "secret"
            mcp_token: str = "mcp"
            api_token: str = "api"
            adb_path: str = "adb"
            allow_shell: bool = False
            rate_limit_per_min: int = 60

        def get_settings() -> Settings:
            return Settings()

        _cfg_mod.DeviceConfig = DeviceConfig
        _cfg_mod.Settings = Settings
        _cfg_mod.get_settings = get_settings
        sys.modules["hub.config"] = _cfg_mod
        import hub
        setattr(hub, "config", _cfg_mod)

from hub.config import DeviceConfig
from hub.devices.adb import AdbError, AdbTimeout, DeviceOffline
from hub.devices.manager import (
    DeviceManager,
    DeviceStatus,
    parse_battery_dumpsys,
    parse_dumpsys_battery,
    parse_dumpsys_screen,
    parse_screen_dumpsys,
)


DUMPSYS_BATTERY_SAMPLE = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  Max charging current: 500000
  Max charging voltage: 5000000
  Charge counter: 2456000
  status: 2
  health: 2
  present: true
  level: 82
  scale: 100
  voltage: 4123
  temperature: 295
  technology: Li-ion
"""

DUMPSYS_POWER_SAMPLE_ON = """
Display Power: state=ON
mWakefulness=Awake
"""

DUMPSYS_POWER_SAMPLE_OFF = """
Display Power: state=OFF
mWakefulness=Asleep
"""


def test_parse_battery_dumpsys():
    # Стандартный вывод Android: 82% и 29.5°C (295 в десятых долях)
    batt, temp = parse_battery_dumpsys(DUMPSYS_BATTERY_SAMPLE)
    assert batt == 82
    assert temp == 29.5

    # 100% и 31.0°C (310)
    batt, temp = parse_dumpsys_battery("level: 100\ntemperature: 310\n")
    assert batt == 100
    assert temp == 31.0

    # Температура уже в градусах (float <= 100)
    batt, temp = parse_battery_dumpsys("level: 50\ntemperature: 28.4\n")
    assert batt == 50
    assert temp == 28.4

    # Низкая температура <= 10.0°C (например, 9.5°C -> 95 в dumpsys)
    batt, temp = parse_battery_dumpsys("level: 70\ntemperature: 95\n")
    assert batt == 70
    assert temp == 9.5

    # 0.0°C -> 0
    batt, temp = parse_battery_dumpsys("level: 70\ntemperature: 0\n")
    assert batt == 70
    assert temp == 0.0

    # Отрицательная температура
    batt, temp = parse_battery_dumpsys("level: 70\ntemperature: -50\n")
    assert batt == 70
    assert temp == -5.0

    # Отсутствуют данные или пустая строка
    assert parse_battery_dumpsys("") == (None, None)
    assert parse_battery_dumpsys("some unrelated text") == (None, None)
    assert parse_battery_dumpsys("level: 45") == (45, None)
    assert parse_battery_dumpsys("temperature: 250") == (None, 25.0)


def test_parse_screen_dumpsys():
    # Display Power: state=ON / OFF
    assert parse_screen_dumpsys(DUMPSYS_POWER_SAMPLE_ON) is True
    assert parse_screen_dumpsys(DUMPSYS_POWER_SAMPLE_OFF) is False

    # mWakefulness
    assert parse_screen_dumpsys("mWakefulness=Awake\n") is True
    assert parse_screen_dumpsys("mWakefulness=Asleep\n") is False
    assert parse_screen_dumpsys("mWakefulness=Dozing\n") is False
    assert parse_screen_dumpsys("mWakefulness=Dreaming\n") is False

    # screenState=SCREEN_STATE_ON / OFF
    assert parse_screen_dumpsys("screenState=SCREEN_STATE_ON") is True
    assert parse_screen_dumpsys("screenState=SCREEN_STATE_OFF") is False

    # isInteractive
    assert parse_dumpsys_screen("isInteractive=true") is True
    assert parse_dumpsys_screen("isInteractive=false") is False

    # Неизвестный формат / пустая строка
    assert parse_screen_dumpsys("") is None
    assert parse_screen_dumpsys("Display Power: unknown") is None

    # Предотвращение ложного срабатывания на mDeviceIdleState=OFF / BatterySaverState=OFF
    dump_with_idle_on = """
    mDeviceIdleState=OFF
    BatterySaverState=OFF
    Display Power: state=ON
    """
    assert parse_screen_dumpsys(dump_with_idle_on) is True

    dump_with_idle_off = """
    mDeviceIdleState=OFF
    BatterySaverState=OFF
    Display Power: state=OFF
    """
    assert parse_screen_dumpsys(dump_with_idle_off) is False


def test_manager_get_and_serial():
    dev1 = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    dev2 = DeviceConfig(id="redmi", name="Redmi Note 11", host="100.64.0.2", port=5555)
    mgr = DeviceManager([dev1, dev2])

    assert mgr.get("realme") == dev1
    assert mgr.get("redmi") == dev2
    assert mgr.serial("realme") == "100.64.0.1:5555"
    assert mgr.serial("redmi") == "100.64.0.2:5555"

    with pytest.raises(KeyError):
        mgr.get("unknown_dev")

    with pytest.raises(KeyError):
        mgr.serial("unknown_dev")


def test_manager_statuses_cache():
    dev1 = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev1])

    statuses = mgr.statuses()
    assert len(statuses) == 1
    st = statuses[0]
    assert st.id == "realme"
    assert st.name == "Realme C21Y"
    assert st.online is False
    assert st.battery is None
    assert st.temperature is None
    assert st.screen_on is None
    assert st.last_seen is None


async def test_manager_refresh_success(monkeypatch):
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev])

    async def _mock_shell(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        if cmd == "dumpsys battery":
            return DUMPSYS_BATTERY_SAMPLE
        if cmd == "dumpsys power":
            return DUMPSYS_POWER_SAMPLE_ON
        return ""

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell)

    status = await mgr.refresh("realme")
    assert status.id == "realme"
    assert status.online is True
    assert status.battery == 82
    assert status.temperature == 29.5
    assert status.screen_on is True
    assert status.last_seen is not None
    assert status.error is None

    # Проверяем, что кеш обновился
    cached = mgr.statuses()[0]
    assert cached.online is True
    assert cached.battery == 82


async def test_manager_refresh_device_offline(monkeypatch):
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev])

    # Сначала делаем устройство успешно опрошенным, чтобы зафиксировать last_seen
    async def _mock_shell_ok(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        if cmd == "dumpsys battery":
            return "level: 90\ntemperature: 270\n"
        return "Display Power: state=ON\n"

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell_ok)
    ok_status = await mgr.refresh("realme")
    assert ok_status.online is True
    first_seen = ok_status.last_seen
    assert first_seen is not None

    # Теперь устройство уходит в оффлайн
    async def _mock_shell_offline(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        raise DeviceOffline("Устройство '100.64.0.1:5555' оффлайн или не найдено")

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell_offline)

    status = await mgr.refresh("realme")
    assert status.online is False
    assert status.battery is None
    assert status.temperature is None
    assert status.screen_on is None
    # last_seen сохраняется с момента последнего успешного опроса
    assert status.last_seen == first_seen
    assert "оффлайн" in str(status.error)


async def test_manager_refresh_timeout(monkeypatch):
    dev = DeviceConfig(id="redmi", name="Redmi Note 11", host="100.64.0.2", port=5555)
    mgr = DeviceManager([dev])

    async def _mock_shell_timeout(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        raise AdbTimeout("Превышено время ожидания команды ADB (15.0 с)")

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell_timeout)

    status = await mgr.refresh("redmi")
    assert status.online is False
    assert status.error is not None
    assert "Превышено время ожидания" in status.error


async def test_manager_auto_reconnect_success(monkeypatch):
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev])

    call_count = 0
    connect_called = False

    async def _mock_shell(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        nonlocal call_count
        call_count += 1
        # Первый вызов падает (устройство оффлайн)
        if call_count <= 1:
            raise DeviceOffline("device offline")
        # После connect возвращает корректные данные
        if cmd == "dumpsys battery":
            return DUMPSYS_BATTERY_SAMPLE
        return DUMPSYS_POWER_SAMPLE_ON

    async def _mock_connect(host: str, port: int | str = 5555, timeout: float = 10.0) -> bool:
        nonlocal connect_called
        connect_called = True
        return True

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell)
    monkeypatch.setattr("hub.devices.manager.connect", _mock_connect)

    status = await mgr.check_device("realme")

    assert connect_called is True
    assert status.online is True
    assert status.battery == 82
    assert status.error is None


async def test_manager_auto_reconnect_failure(monkeypatch):
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev])

    async def _mock_shell(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        raise DeviceOffline("device offline")

    async def _mock_connect(host: str, port: int | str = 5555, timeout: float = 10.0) -> bool:
        return False

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell)
    monkeypatch.setattr("hub.devices.manager.connect", _mock_connect)

    status = await mgr.check_device("realme")
    assert status.online is False
    assert "device offline" in str(status.error)


async def test_manager_start_and_stop(monkeypatch):
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev], interval=0.01)

    checked = 0

    async def _mock_check(dev_id: str) -> DeviceStatus:
        nonlocal checked
        checked += 1
        return DeviceStatus(id=dev_id, name="Realme C21Y", online=True)

    monkeypatch.setattr(mgr, "check_device", _mock_check)

    await mgr.start()
    assert mgr._running is True
    assert mgr._task is not None

    # Даем циклу выполнить хотя бы одну итерацию
    await asyncio.sleep(0.03)
    assert checked >= 1

    await mgr.stop()
    assert mgr._running is False
    assert mgr._task is None


async def test_manager_end_to_end_mock_process(monkeypatch):
    """
    Интеграционный тест: проверяем связку DeviceManager -> adb_shell -> run_adb -> asyncio.create_subprocess_exec.
    """
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev])

    from tests.test_adb import MockSubprocess

    def _exec_dispatcher(*cmd, **kwargs):
        # cmd[0] = "adb", cmd[1] = "-s", cmd[2] = "100.64.0.1:5555", cmd[3] = "shell", cmd[4] = dumpsys ...
        if "dumpsys battery" in cmd:
            return MockSubprocess(stdout=DUMPSYS_BATTERY_SAMPLE.encode(), returncode=0)
        if "dumpsys power" in cmd:
            return MockSubprocess(stdout=DUMPSYS_POWER_SAMPLE_ON.encode(), returncode=0)
        return MockSubprocess(stdout=b"", returncode=0)

    async def _fake_exec(*cmd, **kwargs):
        return _exec_dispatcher(*cmd, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    status = await mgr.refresh("realme")
    assert status.online is True
    assert status.battery == 82
    assert status.temperature == 29.5
    assert status.screen_on is True


async def test_manager_refresh_isolated_power_failure(monkeypatch):
    dev = DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555)
    mgr = DeviceManager([dev])

    async def _mock_shell(serial: str | None, cmd: str, timeout: float = 15.0) -> str:
        if cmd == "dumpsys battery":
            return DUMPSYS_BATTERY_SAMPLE
        if cmd == "dumpsys power":
            raise AdbError("dumpsys power failed: Permission denied")
        return ""

    monkeypatch.setattr("hub.devices.manager.adb_shell", _mock_shell)

    status = await mgr.refresh("realme")
    assert status.id == "realme"
    assert status.online is True
    assert status.battery == 82
    assert status.temperature == 29.5
    assert status.screen_on is None
    assert status.error is None


async def test_manager_parallel_poll(monkeypatch):
    dev1 = DeviceConfig(id="dev1", name="Device 1", host="100.64.0.1", port=5555)
    dev2 = DeviceConfig(id="dev2", name="Device 2", host="100.64.0.2", port=5555)
    dev3 = DeviceConfig(id="dev3", name="Device 3", host="100.64.0.3", port=5555)
    mgr = DeviceManager([dev1, dev2, dev3], interval=0.01)

    concurrent_count = 0
    max_concurrent = 0

    async def _mock_check(dev_id: str) -> DeviceStatus:
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        if concurrent_count > max_concurrent:
            max_concurrent = concurrent_count
        await asyncio.sleep(0.02)
        concurrent_count -= 1
        return DeviceStatus(id=dev_id, name=dev_id, online=True)

    monkeypatch.setattr(mgr, "check_device", _mock_check)

    await mgr.start()
    await asyncio.sleep(0.05)
    await mgr.stop()

    assert max_concurrent > 1



def test_parse_charging_dumpsys():
    from hub.devices.manager import parse_charging_dumpsys

    assert parse_charging_dumpsys("AC powered: false\n  USB powered: true\n") is True
    assert parse_charging_dumpsys("AC powered: false\n  USB powered: false\n  Wireless powered: false\n") is False
    assert parse_charging_dumpsys("level: 50") is None
