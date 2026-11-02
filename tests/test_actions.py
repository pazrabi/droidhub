from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import sys
import types
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

# Подготовка заглушек модулей, если они еще не созданы в этой ветке
if "hub.config" not in sys.modules:
    try:
        import hub.config
    except (ImportError, ModuleNotFoundError):
        mod_config = types.ModuleType("hub.config")

        @dataclass
        class Settings:
            devices_file: str = "config/devices.yaml"
            db_path: str = "data/hub.db"
            admin_user: str = "admin"
            admin_password_hash: str = ""
            session_secret: str = ""
            mcp_token: str = ""
            api_token: str = ""
            adb_path: str = "adb"
            allow_shell: bool = False
            rate_limit_per_min: int = 60

        @dataclass
        class DeviceConfig:
            id: str
            name: str
            host: str
            port: int = 5555

        _settings = Settings()

        def get_settings() -> Settings:
            return _settings

        def load_devices(path: str) -> list[DeviceConfig]:
            return [
                DeviceConfig(id="realme", name="Realme C21Y", host="100.64.0.1", port=5555),
                DeviceConfig(id="redmi", name="Redmi Note 11", host="100.64.0.2", port=5555),
            ]

        mod_config.Settings = Settings  # type: ignore
        mod_config.DeviceConfig = DeviceConfig  # type: ignore
        mod_config.get_settings = get_settings  # type: ignore
        mod_config.load_devices = load_devices  # type: ignore
        sys.modules["hub.config"] = mod_config

if "hub.audit" not in sys.modules:
    try:
        import hub.audit
    except (ImportError, ModuleNotFoundError):
        mod_audit = types.ModuleType("hub.audit")

        class Audit:
            def __init__(self, db_path: str) -> None:
                self.records: list[dict] = []

            async def log(
                self,
                actor: str,
                device_id: str | None,
                action: str,
                params: dict,
                ok: bool,
                error: str | None = None,
            ) -> None:
                self.records.append(
                    {
                        "actor": actor,
                        "device_id": device_id,
                        "action": action,
                        "params": params,
                        "ok": ok,
                        "error": error,
                    }
                )

            async def recent(self, limit: int = 100) -> list[dict]:
                return self.records[-limit:]

        _audit_inst = Audit("data/hub.db")

        def init_audit(db_path: str) -> Audit:
            global _audit_inst
            _audit_inst = Audit(db_path)
            return _audit_inst

        def get_audit() -> Audit:
            return _audit_inst

        mod_audit.Audit = Audit  # type: ignore
        mod_audit.init_audit = init_audit  # type: ignore
        mod_audit.get_audit = get_audit  # type: ignore
        sys.modules["hub.audit"] = mod_audit

if "hub.devices.adb" not in sys.modules:
    try:
        import hub.devices.adb
    except (ImportError, ModuleNotFoundError):
        mod_adb = types.ModuleType("hub.devices.adb")

        class AdbError(Exception):
            pass

        class DeviceOffline(AdbError):
            pass

        class AdbTimeout(AdbError):
            pass

        async def run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False) -> str | bytes:
            return ""

        async def adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
            return ""

        async def connect(host: str, port: int = 5555) -> bool:
            return True

        mod_adb.AdbError = AdbError  # type: ignore
        mod_adb.DeviceOffline = DeviceOffline  # type: ignore
        mod_adb.AdbTimeout = AdbTimeout  # type: ignore
        mod_adb.run_adb = run_adb  # type: ignore
        mod_adb.adb_shell = adb_shell  # type: ignore
        mod_adb.connect = connect  # type: ignore
        sys.modules["hub.devices.adb"] = mod_adb

if "hub.devices.manager" not in sys.modules:
    try:
        import hub.devices.manager
    except (ImportError, ModuleNotFoundError):
        mod_manager = types.ModuleType("hub.devices.manager")

        @dataclass
        class DeviceStatus:
            id: str
            name: str
            online: bool
            battery: int | None = None
            temperature: float | None = None
            screen_on: bool | None = None
            last_seen: float | None = None
            error: str | None = None

        class DeviceManager:
            def __init__(self, devices: list[Any]) -> None:
                self._devices = {d.id: d for d in devices}

            def get(self, device_id: str) -> Any:
                if device_id not in self._devices:
                    raise KeyError(f"Устройство {device_id} не найдено")
                return self._devices[device_id]

            def serial(self, device_id: str) -> str:
                d = self.get(device_id)
                return f"{d.host}:{d.port}"

            def statuses(self) -> list[DeviceStatus]:
                return [DeviceStatus(id=d.id, name=d.name, online=True) for d in self._devices.values()]

        mod_manager.DeviceStatus = DeviceStatus  # type: ignore
        mod_manager.DeviceManager = DeviceManager  # type: ignore
        sys.modules["hub.devices.manager"] = mod_manager

from hub.devices import actions
from hub import cli


class DummyDevice:
    def __init__(self, dev_id: str, host: str = "100.64.0.1", port: int = 5555, name: str = "Realme"):
        self.id = dev_id
        self.host = host
        self.port = port
        self.name = name


class DummyManager:
    def __init__(self) -> None:
        self.devices = {
            "realme": DummyDevice("realme", "100.64.0.1", 5555, "Realme C21Y"),
            "redmi": DummyDevice("redmi", "100.64.0.2", 5555, "Redmi Note 11"),
        }

    def get(self, device_id: str) -> DummyDevice:
        if device_id not in self.devices:
            raise KeyError(f"Устройство {device_id} не найдено")
        return self.devices[device_id]

    def serial(self, device_id: str) -> str:
        dev = self.get(device_id)
        return f"{dev.host}:{dev.port}"

    def statuses(self) -> list[Any]:
        return [
            types.SimpleNamespace(id=d.id, name=d.name, online=True, battery=85)
            for d in self.devices.values()
        ]


class MockAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def log(
        self,
        actor: str,
        device_id: str | None,
        action: str,
        params: dict,
        ok: bool,
        error: str | None = None,
    ) -> None:
        self.entries.append({
            "actor": actor,
            "device_id": device_id,
            "action": action,
            "params": params,
            "ok": ok,
            "error": error,
        })


@pytest.fixture(autouse=True)
def setup_actions(monkeypatch: pytest.MonkeyPatch) -> MockAudit:
    mgr = DummyManager()
    actions.set_manager(mgr)
    mock_audit = MockAudit()
    monkeypatch.setattr(actions, "audit", mock_audit)
    return mock_audit


def make_png_bytes(width: int = 1080, height: int = 2400, color: str = "blue") -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


SAMPLE_UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.android.chrome" content-desc="" clickable="false" bounds="[0,0][1080,2400]">
    <node index="0" text="" resource-id="" class="android.widget.LinearLayout" package="com.android.chrome" content-desc="" clickable="false" bounds="[0,0][1080,2400]">
      <node index="0" text="Search or type URL" resource-id="com.android.chrome:id/search_box" class="android.widget.EditText" package="com.android.chrome" content-desc="Search" clickable="true" bounds="[48,150][1032,270]" />
      <node index="1" text="" resource-id="com.android.chrome:id/tab_switcher" class="android.widget.ImageButton" package="com.android.chrome" content-desc="Tabs" clickable="true" bounds="[900,150][1032,270]" />
      <node index="2" text="Войти" resource-id="com.android.chrome:id/btn_login" class="android.widget.Button" package="com.android.chrome" content-desc="" clickable="true" bounds="[100,500][400,600]" />
      <node index="3" text="Empty Non-interactive" resource-id="" class="android.view.View" package="com.android.chrome" content-desc="" clickable="false" bounds="[0,1000][100,1100]" />
    </node>
  </node>
</hierarchy>
"""


# --- ТЕСТЫ МЕНЕДЖЕРА УСТРОЙСТВ ---

def test_manager_not_set():
    actions.set_manager(None)
    with pytest.raises(RuntimeError, match="Менеджер устройств не установлен"):
        actions.get_manager()


def test_device_not_found():
    with pytest.raises(KeyError, match="не найдено"):
        actions._get_serial("unknown_phone")


# --- ТЕСТ SCREENSHOT ---

@pytest.mark.asyncio
async def test_screenshot_success(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[Any] = []
    sample_png = make_png_bytes(width=1080, height=2400)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append((args, serial, binary))
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    data = await actions.screenshot("realme", max_width=720)
    assert len(calls) == 1
    assert calls[0][0] == ["exec-out", "screencap", "-p"]
    assert calls[0][1] == "100.64.0.1:5555"
    assert calls[0][2] is True

    # Проверка, что результат — валидный JPEG и уменьшен до max_width=720
    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    assert img.width == 720
    assert img.height == 1600

    # Проверка аудита
    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["action"] == "screenshot"
    assert setup_actions.entries[0]["ok"] is True
    assert setup_actions.entries[0]["params"] == {"max_width": 720}


@pytest.mark.asyncio
async def test_screenshot_no_resize_needed(monkeypatch: pytest.MonkeyPatch):
    sample_png = make_png_bytes(width=500, height=1000)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    data = await actions.screenshot("realme", max_width=720)
    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    assert img.width == 500
    assert img.height == 1000


@pytest.mark.asyncio
async def test_screenshot_failure(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    async def mock_run_adb(*args, **kwargs):
        raise RuntimeError("screencap failed")

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    with pytest.raises(RuntimeError, match="screencap failed"):
        await actions.screenshot("realme")

    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["action"] == "screenshot"
    assert setup_actions.entries[0]["ok"] is False
    assert "screencap failed" in setup_actions.entries[0]["error"]


# --- ТЕСТ TAP ---

@pytest.mark.asyncio
async def test_tap(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(f"{serial} -> {cmd}")
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.tap("realme", 150, 300)
    assert calls == ["100.64.0.1:5555 -> input tap 150 300"]
    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["action"] == "tap"
    assert setup_actions.entries[0]["params"] == {"x": 150, "y": 300}
    assert setup_actions.entries[0]["ok"] is True


# --- ТЕСТ SWIPE ---

@pytest.mark.asyncio
async def test_swipe(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.swipe("realme", 100, 200, 300, 400)
    assert calls == ["input swipe 100 200 300 400 300"]

    await actions.swipe("realme", 100, 200, 300, 400, ms=500)
    assert calls[-1] == "input swipe 100 200 300 400 500"
    assert setup_actions.entries[-1]["action"] == "swipe"
    assert setup_actions.entries[-1]["ok"] is True


# --- ТЕСТ LONG_PRESS ---

@pytest.mark.asyncio
async def test_long_press(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.long_press("realme", 250, 450)
    assert calls == ["input swipe 250 450 250 450 800"]

    await actions.long_press("realme", 250, 450, ms=1200)
    assert calls[-1] == "input swipe 250 450 250 450 1200"
    assert setup_actions.entries[-1]["action"] == "long_press"
    assert setup_actions.entries[-1]["ok"] is True


# --- ТЕСТ TYPE_TEXT ---

@pytest.mark.asyncio
async def test_type_text_latin(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Латиница со пробелами должна экранироваться в %s
    await actions.type_text("realme", "Hello World 123")
    assert calls == ["input text Hello%sWorld%s123"]
    assert setup_actions.entries[0]["action"] == "type_text"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_type_text_cyrillic(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Кириллица должна отправляться через ADBKeyBoard broadcast ADB_INPUT_B64
    cyrillic_text = "Привет, мир! 🚀"
    await actions.type_text("realme", cyrillic_text)
    assert len(calls) == 1
    assert "am broadcast -a ADB_INPUT_B64 --es msg" in calls[0]

    # Извлекаем base64 и проверяем декодирование
    b64_part = calls[0].split("--es msg")[-1].strip().strip("'\"")
    decoded = base64.b64decode(b64_part).decode("utf-8")
    assert decoded == cyrillic_text
    assert setup_actions.entries[0]["action"] == "type_text"
    assert setup_actions.entries[0]["ok"] is True


# --- ТЕСТ KEY ---

@pytest.mark.asyncio
async def test_key_known_keys(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    for k, expected_code in [
        ("back", "KEYCODE_BACK"),
        ("home", "KEYCODE_HOME"),
        ("recents", "KEYCODE_APP_SWITCH"),
        ("enter", "KEYCODE_ENTER"),
        ("power", "KEYCODE_POWER"),
        ("volume_up", "KEYCODE_VOLUME_UP"),
        ("volume_down", "KEYCODE_VOLUME_DOWN"),
        ("Home", "KEYCODE_HOME"),  # Регистронезависимость
    ]:
        calls.clear()
        await actions.key("realme", k)
        assert calls == [f"input keyevent {expected_code}"]


@pytest.mark.asyncio
async def test_key_invalid(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="Неизвестная клавиша"):
        await actions.key("realme", "super_unknown_key")


# --- ТЕСТ OPEN_APP ---

@pytest.mark.asyncio
async def test_open_app(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.open_app("realme", "com.android.chrome")
    assert calls == ["monkey -p com.android.chrome -c android.intent.category.LAUNCHER 1"]

    calls.clear()
    await actions.open_app("realme", "com.example/.MainActivity")
    assert calls == ["am start -n com.example/.MainActivity"]

    assert setup_actions.entries[-1]["action"] == "open_app"
    assert setup_actions.entries[-1]["ok"] is True


# --- ТЕСТ LIST_APPS ---

@pytest.mark.asyncio
async def test_list_apps(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    output = """
    package:com.google.android.youtube
    package:com.android.chrome
    package:com.android.settings
    """

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        assert cmd == "pm list packages"
        return output

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    apps = await actions.list_apps("realme")
    assert apps == [
        "com.android.chrome",
        "com.android.settings",
        "com.google.android.youtube",
    ]
    assert setup_actions.entries[0]["action"] == "list_apps"
    assert setup_actions.entries[0]["ok"] is True


# --- ТЕСТ OPEN_URL ---

@pytest.mark.asyncio
async def test_open_url(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.open_url("realme", "https://github.com")
    assert calls == ["am start -a android.intent.action.VIEW -d 'https://github.com'"]
    assert setup_actions.entries[0]["action"] == "open_url"
    assert setup_actions.entries[0]["ok"] is True


# --- ТЕСТ UI_DUMP & TAP_ELEMENT ---

@pytest.mark.asyncio
async def test_ui_dump(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "uiautomator dump" in cmd:
            return "UI hierchary dumped to: /sdcard/window_dump.xml"
        if "cat /sdcard/window_dump.xml" in cmd:
            return SAMPLE_UI_XML
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    elems = await actions.ui_dump("realme")
    # Проверка сжатия: пустые контейнеры FrameLayout/LinearLayout без текста/id/clickable пропущены
    assert len(elems) == 4

    search_box = elems[0]
    assert search_box["text"] == "Search or type URL"
    assert search_box["resource_id"] == "com.android.chrome:id/search_box"
    assert search_box["class"] == "android.widget.EditText"
    assert search_box["content_desc"] == "Search"
    assert search_box["clickable"] is True
    assert search_box["bounds"] == [48, 150, 1032, 270]
    assert search_box["center"] == [(48 + 1032) // 2, (150 + 270) // 2]  # [540, 210]

    btn_login = elems[2]
    assert btn_login["text"] == "Войти"
    assert btn_login["center"] == [250, 550]

    assert setup_actions.entries[0]["action"] == "ui_dump"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_tap_element_by_text(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "cat" in cmd or "dump" in cmd:
            return SAMPLE_UI_XML
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    elem = await actions.tap_element("realme", text="Войти")
    assert elem["text"] == "Войти"
    assert calls == ["input tap 250 550"]


@pytest.mark.asyncio
async def test_tap_element_by_resource_id(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "cat" in cmd or "dump" in cmd:
            return SAMPLE_UI_XML
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    elem = await actions.tap_element("realme", resource_id="tab_switcher")
    assert elem["resource_id"] == "com.android.chrome:id/tab_switcher"
    assert calls == ["input tap 966 210"]


@pytest.mark.asyncio
async def test_tap_element_not_found(monkeypatch: pytest.MonkeyPatch):
    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        return SAMPLE_UI_XML

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(ValueError, match="Элемент не найден"):
        await actions.tap_element("realme", text="Несуществующий текст")


@pytest.mark.asyncio
async def test_tap_element_no_args():
    with pytest.raises(ValueError, match="Необходимо указать text или resource_id"):
        await actions.tap_element("realme")


# --- ТЕСТ WAKE_AND_UNLOCK ---

@pytest.mark.asyncio
async def test_wake_and_unlock(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.wake_and_unlock("realme")
    assert "input keyevent KEYCODE_WAKEUP" in calls
    assert "wm dismiss-keyguard" in calls
    assert "input keyevent 82" in calls
    assert setup_actions.entries[0]["action"] == "wake_and_unlock"
    assert setup_actions.entries[0]["ok"] is True


# --- ТЕСТ SHELL & ALLOW_SHELL ---

@pytest.mark.asyncio
async def test_shell_disallowed(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    class SettingsDisallow:
        allow_shell = False

    monkeypatch.setattr(actions, "get_settings", lambda: SettingsDisallow())

    with pytest.raises(PermissionError, match="allow_shell=False"):
        await actions.shell("realme", "ls /")

    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["action"] == "shell"
    assert setup_actions.entries[0]["ok"] is False


@pytest.mark.asyncio
async def test_shell_allowed(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    class SettingsAllow:
        allow_shell = True

    monkeypatch.setattr(actions, "get_settings", lambda: SettingsAllow())

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        return "root\n"

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    out = await actions.shell("realme", "whoami")
    assert out == "root\n"
    assert setup_actions.entries[0]["action"] == "shell"
    assert setup_actions.entries[0]["ok"] is True


# --- ТЕСТ WAIT ---

@pytest.mark.asyncio
async def test_wait(setup_actions: MockAudit):
    await actions.wait("realme", 0.01)
    assert setup_actions.entries[0]["action"] == "wait"
    assert setup_actions.entries[0]["ok"] is True
    assert setup_actions.entries[0]["params"] == {"seconds": 0.01}


# --- ТЕСТЫ HUB.CLI ---

@pytest.mark.asyncio
async def test_cli_devices(capsys: pytest.CaptureFixture):
    exit_code = await cli.async_main(["devices"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "realme" in captured.out
    assert "redmi" in captured.out


@pytest.mark.asyncio
async def test_cli_tap(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list[tuple[int, int]] = []

    async def mock_tap(device: str, x: int, y: int, **kwargs):
        calls.append((x, y))

    monkeypatch.setattr(actions, "tap", mock_tap)

    exit_code = await cli.async_main(["tap", "realme", "120", "240"])
    assert exit_code == 0
    assert calls == [(120, 240)]
    captured = capsys.readouterr()
    assert "Тап выполнен на realme: (120, 240)" in captured.out


@pytest.mark.asyncio
async def test_cli_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list[str] = []

    async def mock_type_text(device: str, text: str, **kwargs):
        calls.append(text)

    monkeypatch.setattr(actions, "type_text", mock_type_text)

    exit_code = await cli.async_main(["text", "realme", "Привет, мир!"])
    assert exit_code == 0
    assert calls == ["Привет, мир!"]
    captured = capsys.readouterr()
    assert "Текст введён на realme: Привет, мир!" in captured.out


@pytest.mark.asyncio
async def test_cli_screenshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture):
    sample_data = b"fake_jpeg_data"

    async def mock_screenshot(device: str, max_width: int = 720, **kwargs):
        return sample_data

    monkeypatch.setattr(actions, "screenshot", mock_screenshot)

    out_file = tmp_path / "screen.jpg"
    exit_code = await cli.async_main(["screenshot", "realme", str(out_file), "--max-width", "600"])
    assert exit_code == 0
    assert out_file.read_bytes() == sample_data
    captured = capsys.readouterr()
    assert "Скриншот сохранён" in captured.out


@pytest.mark.asyncio
async def test_cli_ui(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    mock_elems = [
        {
            "text": "Войти",
            "resource_id": "com.app:id/login",
            "class": "android.widget.Button",
            "content_desc": "",
            "clickable": True,
            "bounds": [10, 20, 100, 50],
            "center": [55, 35],
        }
    ]

    async def mock_ui_dump(device: str, **kwargs):
        return mock_elems

    monkeypatch.setattr(actions, "ui_dump", mock_ui_dump)

    # Вывод обычный
    exit_code = await cli.async_main(["ui", "realme"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Найдено элементов на realme: 1" in captured.out
    assert "android.widget.Button" in captured.out

    # Вывод JSON
    exit_code_json = await cli.async_main(["ui", "realme", "--json"])
    assert exit_code_json == 0
    captured_json = capsys.readouterr()
    assert '"text": "Войти"' in captured_json.out


@pytest.mark.asyncio
async def test_cli_error_handling(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    async def mock_tap(*args, **kwargs):
        raise RuntimeError("Устройство оффлайн")

    monkeypatch.setattr(actions, "tap", mock_tap)

    exit_code = await cli.async_main(["tap", "realme", "10", "20"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Ошибка: Устройство оффлайн" in captured.err
