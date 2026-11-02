from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import shlex
import sys
import time
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
    import time
    actions._screen_cache.clear()
    actions._screen_cache["realme"] = (True, time.monotonic())
    actions._screen_cache["redmi"] = (True, time.monotonic())
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
        if args and args[0] == "pull":
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    data = await actions.screenshot("realme", max_width=720)
    assert len(calls) == 3
    assert calls[0][0] == ["shell", "screencap -p /data/local/tmp/hub_100_64_0_1_5555.png"]
    assert calls[0][1] == "100.64.0.1:5555"
    assert calls[1][0] == ["pull", "/data/local/tmp/hub_100_64_0_1_5555.png", calls[1][0][2]]
    assert calls[1][1] == "100.64.0.1:5555"
    assert calls[2][0] == ["shell", "rm -f /data/local/tmp/hub_100_64_0_1_5555.png"]
    assert calls[2][1] == "100.64.0.1:5555"

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
async def test_screenshot_fallback_exec_out(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[Any] = []
    sample_png = make_png_bytes(width=1080, height=2400)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append((args, serial, binary))
        if args and args[0] == "pull":
            raise RuntimeError("adb pull failed")
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    data = await actions.screenshot("realme", max_width=720)
    exec_out_calls = [c for c in calls if c[0] == ["exec-out", "screencap", "-p"]]
    assert len(exec_out_calls) == 1
    assert exec_out_calls[0][1] == "100.64.0.1:5555"
    assert exec_out_calls[0][2] is True

    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    assert img.width == 720
    assert img.height == 1600


@pytest.mark.asyncio
async def test_screenshot_concurrency(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[Any] = []
    sample_png = make_png_bytes(width=1080, height=2400)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append((args, serial, binary))
        if args and args[0] == "pull":
            await asyncio.sleep(0.05)
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    # Запускаем два одновременных скриншота на одно устройство
    res1, res2 = await asyncio.gather(
        actions.screenshot("realme", max_width=720),
        actions.screenshot("realme", max_width=720),
    )

    # Параллельные запросы получают одинаковый результат текущего скриншота
    assert res1 == res2

    # adb pull был вызван ровно один раз
    pull_calls = [c for c in calls if c[0] and c[0][0] == "pull"]
    assert len(pull_calls) == 1

    # Оба запроса записали аудит
    assert len(setup_actions.entries) == 2
    assert all(e["action"] == "screenshot" and e["ok"] is True for e in setup_actions.entries)


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
    monkeypatch.setattr(actions, "_IME_SWITCH_DELAY", 0.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if "pm list packages" in cmd:
            return "package:com.android.adbkeyboard\n"
        if "settings get secure default_input_method" in cmd:
            return "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Кириллица должна отправляться через ADBKeyBoard broadcast ADB_INPUT_B64 с переключением IME
    cyrillic_text = "Привет, мир! 🚀"
    await actions.type_text("realme", cyrillic_text)
    
    broadcast_calls = [c for c in calls if "am broadcast -a ADB_INPUT_B64 --es msg" in c]
    assert len(broadcast_calls) == 1

    # Извлекаем base64 и проверяем декодирование
    b64_part = broadcast_calls[0].split("--es msg")[-1].strip().strip("'\"")
    decoded = base64.b64decode(b64_part).decode("utf-8")
    assert decoded == cyrillic_text
    assert setup_actions.entries[0]["action"] == "type_text"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_type_text_special_characters(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Текст со спецсимволами shell: &, $, кавычки, точка с запятой, перенаправления
    special_text = 'test & id; echo "hello $USER" && rm -rf / < > `whoami`'
    await actions.type_text("realme", special_text)
    assert len(calls) == 1
    # Чисто ASCII текст передается через input text с экранированием пробелов в %s и shlex.quote
    escaped = special_text.replace(" ", "%s")
    assert calls[0] == f"input text {shlex.quote(escaped)}"
    assert setup_actions.entries[0]["action"] == "type_text"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_type_text_command_order(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    monkeypatch.setattr(actions, "_IME_SWITCH_DELAY", 0.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if "pm list packages" in cmd:
            return "package:com.android.adbkeyboard\n"
        if "settings get secure default_input_method" in cmd:
            return "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.type_text("realme", "Привет мир")

    assert calls == [
        "pm list packages com.android.adbkeyboard",
        "settings get secure default_input_method",
        "ime enable com.android.adbkeyboard/.AdbIME",
        "ime set com.android.adbkeyboard/.AdbIME",
        f"am broadcast -a ADB_INPUT_B64 --es msg '{base64.b64encode('Привет мир'.encode('utf-8')).decode('ascii')}'",
        "ime set com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME",
    ]
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_type_text_restore_ime_on_exception(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    monkeypatch.setattr(actions, "_IME_SWITCH_DELAY", 0.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if "pm list packages" in cmd:
            return "package:com.android.adbkeyboard\n"
        if "settings get secure default_input_method" in cmd:
            return "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
        if "am broadcast" in cmd:
            raise actions.AdbError("Ошибка широковещательной рассылки")
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.AdbError, match="Ошибка широковещательной рассылки"):
        await actions.type_text("realme", "Привет с ошибкой")

    # В блоке finally прежняя клавиатура должна быть обязательно возвращена
    assert calls[-1] == "ime set com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
    assert setup_actions.entries[0]["ok"] is False
    assert "Ошибка широковещательной рассылки" in setup_actions.entries[0]["error"]


@pytest.mark.asyncio
async def test_type_text_restore_ime_skipped_if_already_adbime(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    monkeypatch.setattr(actions, "_IME_SWITCH_DELAY", 0.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if "pm list packages" in cmd:
            return "package:com.android.adbkeyboard\n"
        if "settings get secure default_input_method" in cmd:
            return "com.android.adbkeyboard/.AdbIME\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.type_text("realme", "Привет")
    # Если текущая клавиатура уже была AdbIME, в finally повторный ime set не вызывается
    assert not any(c.startswith("ime set") and "com.android.adbkeyboard/.AdbIME" not in c for c in calls)
    assert calls[-1].startswith("am broadcast")


@pytest.mark.asyncio
async def test_type_text_missing_adbkeyboard_package(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if "pm list packages" in cmd:
            return ""  # Пакет не найден
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(RuntimeError, match="На телефоне не установлена клавиатура ADBKeyBoard"):
        await actions.type_text("realme", "Привет, проверка!")

    # Должна выполниться только проверка пакета, без переключения клавиатуры
    assert calls == ["pm list packages com.android.adbkeyboard"]
    assert setup_actions.entries[0]["ok"] is False
    assert "На телефоне не установлена клавиатура ADBKeyBoard" in setup_actions.entries[0]["error"]


@pytest.mark.asyncio
async def test_type_text_ascii_no_ime_switch(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    ascii_text = "Quick brown fox jumps over 123 lazy dogs!"
    await actions.type_text("realme", ascii_text)

    # Чисто ASCII-текст отправляется через input text без вызова pm list packages, settings get или ime set
    assert calls == [f"input text {shlex.quote(ascii_text.replace(' ', '%s'))}"]
    assert not any("ime" in c for c in calls)
    assert not any("package" in c for c in calls)
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_type_text_long_text_chunks(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    monkeypatch.setattr(actions, "_IME_SWITCH_DELAY", 0.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if "pm list packages" in cmd:
            return "package:com.android.adbkeyboard\n"
        if "settings get secure default_input_method" in cmd:
            return "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Длинный текст на кириллице (более 1000 байт), который должен разбиться на части <= 800 символов base64
    long_cyrillic_text = "Привет, мир! Это длинный тестовый текст для проверки отправки частями. " * 15
    await actions.type_text("realme", long_cyrillic_text)

    broadcast_calls = [c for c in calls if "am broadcast -a ADB_INPUT_B64 --es msg" in c]
    assert len(broadcast_calls) > 1

    decoded_parts: list[str] = []
    for call in broadcast_calls:
        b64_part = call.split("--es msg")[-1].strip().strip("'\"")
        assert len(b64_part) <= 800
        decoded_parts.append(base64.b64decode(b64_part).decode("utf-8"))

    assert "".join(decoded_parts) == long_cyrillic_text
    # В конце клавиатура должна быть возвращена на место
    assert calls[-1] == "ime set com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_type_text_device_lock(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    monkeypatch.setattr(actions, "_IME_SWITCH_DELAY", 0.0)

    active_count = 0
    max_active = 0

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal active_count, max_active
        calls.append(cmd)
        if "pm list packages" in cmd:
            return "package:com.android.adbkeyboard\n"
        if "settings get secure default_input_method" in cmd:
            return "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
        if "am broadcast" in cmd:
            active_count += 1
            max_active = max(max_active, active_count)
            await asyncio.sleep(0.01)
            active_count -= 1
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Запускаем два параллельных ввода с не-ASCII текстом на одном устройстве
    await asyncio.gather(
        actions.type_text("realme", "Первый текст 1"),
        actions.type_text("realme", "Второй текст 2"),
    )

    # Благодаря lock на устройство, они не должны выполняться одновременно
    assert max_active == 1

    # Проверяем, что блокировки создаются индивидуально под device_id
    lock_realme = actions._get_type_text_lock("realme")
    lock_redmi = actions._get_type_text_lock("redmi")
    assert lock_realme is not lock_redmi


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
        import time
        actions._screen_cache["realme"] = (True, time.monotonic())
        await actions.key("realme", k)
        assert calls == [f"input keyevent {expected_code}"]


@pytest.mark.asyncio
async def test_key_invalid(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="Неизвестная клавиша"):
        await actions.key("realme", "super_unknown_key")


@pytest.mark.asyncio
async def test_key_injection_and_validation(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="Неизвестная клавиша"):
        await actions.key("realme", "KEYCODE_BACK; reboot")

    with pytest.raises(ValueError, match="Неизвестная клавиша"):
        await actions.key("realme", "KEYCODE_HOME && id")


@pytest.mark.asyncio
async def test_key_valid_keycode(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.key("realme", "KEYCODE_VOLUME_UP")
    assert calls == ["input keyevent KEYCODE_VOLUME_UP"]


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


@pytest.mark.asyncio
async def test_open_app_escaping(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    pkg_with_inject = "com.example.app; reboot"
    await actions.open_app("realme", pkg_with_inject)
    assert calls == [f"monkey -p {shlex.quote(pkg_with_inject)} -c android.intent.category.LAUNCHER 1"]

    calls.clear()
    comp_with_inject = "com.example/MainActivity; reboot"
    await actions.open_app("realme", comp_with_inject)
    assert calls == [f"am start -n {shlex.quote(comp_with_inject)}"]


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
    assert calls == [f"am start -a android.intent.action.VIEW -d {shlex.quote('https://github.com')}"]
    assert setup_actions.entries[0]["action"] == "open_url"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_open_url_special_characters(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    url_with_specials = "https://example.com/search?q=it's+a+test&foo=bar$baz;id"
    await actions.open_url("realme", url_with_specials)
    assert calls == [f"am start -a android.intent.action.VIEW -d {shlex.quote(url_with_specials)}"]
    # Проверяем, что выход из кавычек и инъекция не произошли
    assert ";id" not in calls[0].split(shlex.quote(url_with_specials))[0]
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


@pytest.mark.asyncio
async def test_ui_dump_idle_error(monkeypatch: pytest.MonkeyPatch):
    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "uiautomator dump" in cmd:
            return "ERROR: could not get idle state"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(RuntimeError, match="Не удалось получить дамп UI: ERROR: could not get idle state"):
        await actions.ui_dump("realme")


@pytest.mark.asyncio
async def test_ui_dump_no_hierarchy(monkeypatch: pytest.MonkeyPatch):
    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "cat" in cmd:
            return "cat: /sdcard/window_dump.xml: No such file or directory"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(RuntimeError, match="Не удалось получить дамп UI"):
        await actions.ui_dump("realme")


@pytest.mark.asyncio
async def test_ui_dump_cleans_control_chars(monkeypatch: pytest.MonkeyPatch):
    xml_with_ctrl = SAMPLE_UI_XML.replace("Search or type URL", "Search\x00\x08\x0b\x0c URL")

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "cat" in cmd or "dump" in cmd:
            return xml_with_ctrl
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    elems = await actions.ui_dump("realme")
    assert len(elems) == 4
    assert elems[0]["text"] == "Search URL"


@pytest.mark.asyncio
async def test_tap_element_empty_string(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "cat" in cmd or "dump" in cmd:
            return SAMPLE_UI_XML
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Передача text="" не должна ломать поиск по resource_id
    elem = await actions.tap_element("realme", text="", resource_id="btn_login")
    assert elem["resource_id"] == "com.android.chrome:id/btn_login"
    assert elem["text"] == "Войти"
    assert calls == ["input tap 250 550"]


@pytest.mark.asyncio
async def test_tap_element_prioritizes_visible(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    xml_with_invisible_first = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" clickable="false" bounds="[0,0][1080,2400]">
    <node index="0" text="Submit" resource-id="com.app:id/submit" class="android.widget.Button" clickable="true" bounds="[0,0][0,0]" />
    <node index="1" text="Submit" resource-id="com.app:id/submit" class="android.widget.Button" clickable="true" bounds="[100,200][300,300]" />
  </node>
</hierarchy>
"""

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "cat" in cmd or "dump" in cmd:
            return xml_with_invisible_first
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Должен выбрать видимый элемент с center=[200, 250], а не шаблонный [0, 0]
    elem = await actions.tap_element("realme", text="Submit")
    assert elem["center"] == [200, 250]
    assert calls == ["input tap 200 250"]


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


@pytest.mark.asyncio
async def test_cli_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list[str] = []

    async def mock_key(device: str, name: str, **kwargs):
        calls.append(name)

    monkeypatch.setattr(actions, "key", mock_key)

    exit_code = await cli.async_main(["key", "realme", "home"])
    assert exit_code == 0
    assert calls == ["home"]
    captured = capsys.readouterr()
    assert "Клавиша 'home' нажата на realme" in captured.out


@pytest.mark.asyncio
async def test_cli_swipe(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list[Any] = []

    async def mock_swipe(device: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300, **kwargs):
        calls.append((x1, y1, x2, y2, ms))

    monkeypatch.setattr(actions, "swipe", mock_swipe)

    exit_code = await cli.async_main(["swipe", "realme", "100", "200", "300", "400", "--ms", "500"])
    assert exit_code == 0
    assert calls == [(100, 200, 300, 400, 500)]
    captured = capsys.readouterr()
    assert "Свайп выполнен на realme: (100, 200) -> (300, 400)" in captured.out


@pytest.mark.asyncio
async def test_cli_open_app(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list[str] = []

    async def mock_open_app(device: str, package: str, **kwargs):
        calls.append(package)

    monkeypatch.setattr(actions, "open_app", mock_open_app)

    exit_code = await cli.async_main(["open_app", "realme", "com.android.chrome"])
    assert exit_code == 0
    assert calls == ["com.android.chrome"]
    captured = capsys.readouterr()
    assert "Приложение 'com.android.chrome' запущено на realme" in captured.out


@pytest.mark.asyncio
async def test_cli_open_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list[str] = []

    async def mock_open_url(device: str, url: str, **kwargs):
        calls.append(url)

    monkeypatch.setattr(actions, "open_url", mock_open_url)

    exit_code = await cli.async_main(["open_url", "realme", "https://github.com"])
    assert exit_code == 0
    assert calls == ["https://github.com"]
    captured = capsys.readouterr()
    assert "URL 'https://github.com' открыт на realme" in captured.out


@pytest.mark.asyncio
async def test_cli_devices_empty_statuses(capsys: pytest.CaptureFixture):
    class EmptyStatusManager(DummyManager):
        def statuses(self) -> list[Any]:
            return []

    mgr = EmptyStatusManager()
    actions.set_manager(mgr)

    exit_code = await cli.async_main(["devices"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "realme: Realme C21Y" in captured.out
    assert "redmi: Redmi Note 11" in captured.out
    assert "Нет доступных устройств" not in captured.out


# --- ТЕСТЫ АВТОПОДКЛЮЧЕНИЯ (AUTO-RECONNECT) ---

@pytest.mark.asyncio
async def test_auto_reconnect_on_device_offline(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    connect_calls: list[tuple[str, int]] = []
    shell_calls: list[str] = []
    first_call = True

    async def mock_connect(host: str, port: int = 5555) -> bool:
        connect_calls.append((host, port))
        return True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal first_call
        if first_call:
            first_call = False
            raise actions.DeviceOffline("error: device offline")
        shell_calls.append(f"{serial} -> {cmd}")
        return ""

    monkeypatch.setattr(actions, "connect", mock_connect)
    if hasattr(actions, "adb") and actions.adb is not None:
        monkeypatch.setattr(actions.adb, "connect", mock_connect)
    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.tap("realme", 100, 200)

    # Проверяем, что connect вызван для нужного устройства (host, port)
    assert connect_calls == [("100.64.0.1", 5555)]
    # Действие повторено и выполнено успешно
    assert shell_calls == ["100.64.0.1:5555 -> input tap 100 200"]
    # В аудите 1 запись с успехом
    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["action"] == "tap"
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_auto_reconnect_on_screenshot(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    connect_calls: list[tuple[str, int]] = []
    calls: list[Any] = []
    sample_png = make_png_bytes(width=1080, height=2400)
    first_call = True

    async def mock_connect(host: str, port: int = 5555) -> bool:
        connect_calls.append((host, port))
        return True

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        nonlocal first_call
        if first_call:
            first_call = False
            raise actions.DeviceOffline("error: device offline")
        calls.append((args, serial, binary))
        if args and args[0] == "pull":
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "connect", mock_connect)
    if hasattr(actions, "adb") and actions.adb is not None:
        monkeypatch.setattr(actions.adb, "connect", mock_connect)
    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    data = await actions.screenshot("realme", max_width=720)
    assert connect_calls == [("100.64.0.1", 5555)]
    # 1 rm (из finally первой попытки) + 1 screencap + 1 pull + 1 rm (из finally второй попытки)
    assert len(calls) == 4
    assert calls[1][0] == ["shell", "screencap -p /data/local/tmp/hub_100_64_0_1_5555.png"]
    assert calls[2][0][0] == "pull"
    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_auto_reconnect_failure_raises(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    connect_calls: list[tuple[str, int]] = []

    async def mock_connect(host: str, port: int = 5555) -> bool:
        connect_calls.append((host, port))
        return False

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        raise actions.DeviceOffline("error: device offline")

    monkeypatch.setattr(actions, "connect", mock_connect)
    if hasattr(actions, "adb") and actions.adb is not None:
        monkeypatch.setattr(actions.adb, "connect", mock_connect)
    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.DeviceOffline, match="device offline"):
        await actions.tap("realme", 100, 200)

    # connect вызван ровно один раз
    assert connect_calls == [("100.64.0.1", 5555)]
    # Аудит зафиксировал ошибку
    assert len(setup_actions.entries) == 1
    assert setup_actions.entries[0]["action"] == "tap"
    assert setup_actions.entries[0]["ok"] is False


@pytest.mark.asyncio
async def test_auto_reconnect_not_called_on_non_offline_error(monkeypatch: pytest.MonkeyPatch):
    connect_calls: list[Any] = []

    async def mock_connect(host: str, port: int = 5555) -> bool:
        connect_calls.append((host, port))
        return True

    monkeypatch.setattr(actions, "connect", mock_connect)

    with pytest.raises(ValueError, match="Неизвестная клавиша"):
        await actions.key("realme", "super_invalid_key")

    assert len(connect_calls) == 0


@pytest.mark.asyncio
async def test_screenshot_serial_normalization(monkeypatch: pytest.MonkeyPatch):
    calls: list[Any] = []
    sample_png = make_png_bytes(width=100, height=100)

    class CustomDeviceManager(DummyManager):
        def serial(self, device_id: str) -> str:
            return "192.168.1.50:5555/extra:path test"

    mgr = CustomDeviceManager()
    actions.set_manager(mgr)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append(args)
        if args and args[0] == "pull":
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    await actions.screenshot("realme", max_width=100)
    # Имя файла на телефоне должно содержать только безопасные символы
    remote_path = calls[1][1]
    assert remote_path == "/data/local/tmp/hub_192_168_1_50_5555_extra_path_test.png"


@pytest.mark.asyncio
async def test_screenshot_concurrency_different_devices(monkeypatch: pytest.MonkeyPatch):
    sample_png = make_png_bytes(width=200, height=200)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        if args and args[0] == "pull":
            await asyncio.sleep(0.02)
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    # Запускаем параллельные скриншоты для двух разных устройств — они не блокируют друг друга
    r1, r2 = await asyncio.gather(
        actions.screenshot("realme", max_width=200),
        actions.screenshot("redmi", max_width=200),
    )
    assert len(r1) > 0
    assert len(r2) > 0


@pytest.mark.asyncio
async def test_screenshot_concurrency_failure_propagates(monkeypatch: pytest.MonkeyPatch):
    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        await asyncio.sleep(0.02)
        raise RuntimeError("hardware screencap error")

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    results = await asyncio.gather(
        actions.screenshot("realme", max_width=200),
        actions.screenshot("realme", max_width=200),
        return_exceptions=True,
    )
    assert len(results) == 2
    assert isinstance(results[0], RuntimeError) and "hardware screencap error" in str(results[0])
    assert isinstance(results[1], RuntimeError) and "hardware screencap error" in str(results[1])


@pytest.mark.asyncio
async def test_screenshot_concurrency_different_max_widths(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[Any] = []
    sample_png = make_png_bytes(width=1000, height=2000)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append(args)
        if args and args[0] == "pull":
            await asyncio.sleep(0.02)
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    # Запускаем два одновременных скриншота с разными max_width
    r1, r2 = await asyncio.gather(
        actions.screenshot("realme", max_width=200),
        actions.screenshot("realme", max_width=500),
    )

    img1 = Image.open(BytesIO(r1))
    img2 = Image.open(BytesIO(r2))

    assert img1.width == 200
    assert img2.width == 500

    pull_calls = [c for c in calls if c and c[0] == "pull"]
    assert len(pull_calls) == 1

    assert len(setup_actions.entries) == 2
    params_widths = [e["params"]["max_width"] for e in setup_actions.entries]
    assert 200 in params_widths
    assert 500 in params_widths


@pytest.mark.asyncio
async def test_screenshot_concurrency_initiator_cancellation(monkeypatch: pytest.MonkeyPatch):
    sample_png = make_png_bytes(width=800, height=1200)

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        if args and args[0] == "pull":
            await asyncio.sleep(0.05)
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    t1 = asyncio.create_task(actions.screenshot("realme", max_width=400))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(actions.screenshot("realme", max_width=600))

    # Отменяем первый запрос (инициатор)
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # Второй параллельный запрос должен успешно завершиться
    res2 = await t2
    img2 = Image.open(BytesIO(res2))
    assert img2.width == 600


@pytest.mark.asyncio
async def test_raw_screenshot_pull_cleanup_on_cancellation(monkeypatch: pytest.MonkeyPatch):
    calls: list[Any] = []
    pull_started = asyncio.Event()

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append(args)
        if args and args[0] == "pull":
            pull_started.set()
            await asyncio.sleep(1.0)
        return b""

    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    t = asyncio.create_task(actions._raw_screenshot_pull("realme"))
    await pull_started.wait()
    t.cancel()

    with pytest.raises(asyncio.CancelledError):
        await t

    # Проверяем, что в finally был вызван rm -f
    rm_calls = [c for c in calls if len(c) > 1 and "rm -f" in c[1]]
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_tap_element_no_redundant_reconnect_on_offline(monkeypatch: pytest.MonkeyPatch):
    connect_calls: list[tuple[str, int]] = []

    async def mock_connect(host: str, port: int = 5555) -> bool:
        connect_calls.append((host, port))
        return False

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        raise actions.DeviceOffline("error: device offline")

    monkeypatch.setattr(actions, "connect", mock_connect)
    if hasattr(actions, "adb") and actions.adb is not None:
        monkeypatch.setattr(actions.adb, "connect", mock_connect)
    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.DeviceOffline):
        await actions.tap_element("realme", text="Войти")

    # Переподключение должно вызываться ровно 1 раз (из ui_dump), без повторов на уровне tap_element
    assert len(connect_calls) == 1


# --- ТЕСТЫ ЭТАПА 3.5: ENSURE_AWAKE И POWER ---

def test_parse_screen_off():
    # Android 11: Display Power: state=ON/OFF, mWakefulness=Awake/Asleep
    assert actions._parse_screen_off("Display Power: state=ON\nmWakefulness=Awake\n") is False
    assert actions._parse_screen_off("Display Power: state=OFF\nmWakefulness=Asleep\n") is True
    assert actions._parse_screen_off("Display Power: state=DOZE\nmWakefulness=Dozing\n") is True
    assert actions._parse_screen_off("Display Power: state=OFF\n") is True
    assert actions._parse_screen_off("Display Power: state=ON\n") is False

    # Android 13: mWakefulness=Awake / Asleep / Dozing / Dreaming
    assert actions._parse_screen_off("mWakefulness=Awake\n") is False
    assert actions._parse_screen_off("mWakefulness=Asleep\n") is True
    assert actions._parse_screen_off("mWakefulness=Dozing\n") is True
    assert actions._parse_screen_off("mWakefulness=Dreaming\n") is True

    # Смешанные / граничные состояния
    assert actions._parse_screen_off("mWakefulness=Awake\nDisplay Power: state=OFF\n") is True
    assert actions._parse_screen_off("mWakefulness=Asleep\nDisplay Power: state=ON\n") is True
    assert actions._parse_screen_off("") is False


@pytest.mark.asyncio
async def test_ensure_awake_already_awake(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return "mWakefulness=Awake\nDisplay Power: state=ON\n"

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # 1. Первый вызов — проверка через dumpsys
    res = await actions.ensure_awake("realme")
    assert res is False
    assert calls == ['dumpsys power | grep -E "mWakefulness|Display Power"']
    assert actions._is_screen_cached_awake("realme") is True

    # 2. Второй вызов в пределах 3 с — попадание в кеш, adb_shell не вызывается
    res2 = await actions.ensure_awake("realme")
    assert res2 is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ensure_awake_wakes_device_android11(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "Display Power: state=OFF\nmWakefulness=Asleep\n"
            return "Display Power: state=ON\nmWakefulness=Awake\n"
        if "input keyevent KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    res = await actions.ensure_awake("realme")
    assert res is True
    assert "input keyevent KEYCODE_WAKEUP" in calls
    assert actions._is_screen_cached_awake("realme") is True


@pytest.mark.asyncio
async def test_ensure_awake_wakes_device_android13(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "input keyevent KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    res = await actions.ensure_awake("realme")
    assert res is True
    assert "input keyevent KEYCODE_WAKEUP" in calls
    assert actions._is_screen_cached_awake("realme") is True


@pytest.mark.asyncio
async def test_ensure_awake_with_swipe_lock(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "dumpsys window" in cmd:
            return "mShowing=true\n"
        if "input keyevent KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    res = await actions.ensure_awake("realme")
    assert res is True
    assert "input keyevent KEYCODE_WAKEUP" in calls
    assert any("input swipe" in c for c in calls)
    assert any("wm dismiss-keyguard" in c for c in calls)


@pytest.mark.asyncio
async def test_ensure_awake_cache_expiry(monkeypatch: pytest.MonkeyPatch):
    import time

    calls: list[str] = []
    actions._screen_cache.clear()
    # Кеш устарел (> 3.0 с)
    actions._screen_cache["realme"] = (True, time.monotonic() - 4.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return "mWakefulness=Awake\n"

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    res = await actions.ensure_awake("realme")
    assert res is False
    assert len(calls) == 1  # Вызвал adb_shell из-за истечения срока кеша


@pytest.mark.asyncio
async def test_ensure_awake_recursion_protection():
    # Защита от рекурсии: если устройство уже в обработке _in_ensure_awake, возвращает False
    actions._in_ensure_awake.add("realme")
    try:
        res = await actions.ensure_awake("realme")
        assert res is False
    finally:
        actions._in_ensure_awake.discard("realme")


@pytest.mark.asyncio
async def test_power_wake(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.power("realme", "wake")
    assert calls == ["input keyevent KEYCODE_WAKEUP"]
    assert actions._is_screen_cached_awake("realme") is True
    assert setup_actions.entries[-1]["action"] == "power:wake"
    assert setup_actions.entries[-1]["params"] == {"action": "wake"}
    assert setup_actions.entries[-1]["ok"] is True


@pytest.mark.asyncio
async def test_power_sleep(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.power("realme", "sleep")
    assert calls == ["input keyevent KEYCODE_SLEEP"]
    assert actions._is_screen_cached_awake("realme") is False
    assert setup_actions.entries[-1]["action"] == "power:sleep"
    assert setup_actions.entries[-1]["params"] == {"action": "sleep"}
    assert setup_actions.entries[-1]["ok"] is True


@pytest.mark.asyncio
async def test_power_reboot(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    actions._screen_cache.clear()
    actions._screen_cache["realme"] = (True, 100.0)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.power("realme", "reboot")
    assert calls == ["reboot"]
    assert "realme" not in actions._screen_cache
    assert setup_actions.entries[-1]["action"] == "power:reboot"
    assert setup_actions.entries[-1]["params"] == {"action": "reboot"}
    assert setup_actions.entries[-1]["ok"] is True


@pytest.mark.asyncio
async def test_power_shutdown_reboot_p_success(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return "Shutdown initialized\n"

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.power("realme", "shutdown")
    assert calls == ["reboot -p"]
    assert setup_actions.entries[-1]["action"] == "power:shutdown"
    assert setup_actions.entries[-1]["ok"] is True


@pytest.mark.asyncio
async def test_power_shutdown_empty_fallback_to_svc(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if cmd == "reboot -p":
            return ""  # Пустой результат -> fallback на svc power shutdown
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.power("realme", "shutdown")
    assert calls == ["reboot -p", "svc power shutdown"]
    assert setup_actions.entries[-1]["action"] == "power:shutdown"
    assert setup_actions.entries[-1]["ok"] is True


@pytest.mark.asyncio
async def test_power_shutdown_error_fallback_to_svc(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        if cmd == "reboot -p":
            raise actions.AdbError("reboot: Operation not permitted")
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.power("realme", "shutdown")
    assert calls == ["reboot -p", "svc power shutdown"]
    assert setup_actions.entries[-1]["action"] == "power:shutdown"
    assert setup_actions.entries[-1]["ok"] is True


@pytest.mark.asyncio
async def test_power_invalid_action():
    with pytest.raises(ValueError, match="Неизвестное действие питания"):
        await actions.power("realme", "hibernate")


@pytest.mark.asyncio
async def test_power_does_not_call_ensure_awake(monkeypatch: pytest.MonkeyPatch):
    awake_called = False

    async def mock_ensure_awake(*args, **kwargs):
        nonlocal awake_called
        awake_called = True
        return False

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        return "ok\n"

    monkeypatch.setattr(actions, "ensure_awake", mock_ensure_awake)
    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    for act in ("wake", "sleep", "reboot", "shutdown"):
        await actions.power("realme", act)

    assert awake_called is False


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_tap(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.tap("realme", 100, 200)
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert calls[-1] == "input tap 100 200"


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_swipe(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.swipe("realme", 10, 20, 30, 40)
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert calls[-1] == "input swipe 10 20 30 40 300"


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_long_press(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.long_press("realme", 15, 25)
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert "input swipe 15 25 15 25 800" in calls[-1]


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_type_text(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.type_text("realme", "Hello")
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert "input text Hello" in calls[-1]


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_open_app(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.open_app("realme", "com.android.chrome")
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert any("monkey -p com.android.chrome" in c for c in calls)


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_open_url(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.open_url("realme", "https://github.com")
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert any("android.intent.action.VIEW" in c for c in calls)


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_ui_dump(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        if "uiautomator dump" in cmd:
            return "UI hierchary dumped to: /sdcard/window_dump.xml"
        if "cat /sdcard/window_dump.xml" in cmd:
            return SAMPLE_UI_XML
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    elems = await actions.ui_dump("realme")
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert len(elems) > 0


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_key_non_power(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(cmd)
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.key("realme", "home")
    assert any("input keyevent KEYCODE_WAKEUP" in c for c in calls)
    assert calls[-1] == "input keyevent KEYCODE_HOME"


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_key_power_skips_ensure_awake(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Для name="power", "Power", "KEYCODE_POWER" ensure_awake не должен вызываться
    await actions.key("realme", "power")
    assert calls == ["input keyevent KEYCODE_POWER"]

    calls.clear()
    await actions.key("realme", "Power")
    assert calls == ["input keyevent KEYCODE_POWER"]

    calls.clear()
    await actions.key("realme", "KEYCODE_POWER")
    assert calls == ["input keyevent KEYCODE_POWER"]


@pytest.mark.asyncio
async def test_auto_ensure_awake_in_screenshot(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    actions._screen_cache.clear()
    state_asleep = True
    sample_png = make_png_bytes(width=200, height=200)

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        calls.append(f"shell: {cmd}")
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            state_asleep = False
            return ""
        return ""

    async def mock_run_adb(args: list[str], *, serial: str | None = None, timeout: float = 15.0, binary: bool = False):
        calls.append(f"run_adb: {' '.join(args)}")
        if args and args[0] == "pull":
            Path(args[2]).write_bytes(sample_png)
        return sample_png

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)
    monkeypatch.setattr(actions, "run_adb", mock_run_adb)

    data = await actions.screenshot("realme", max_width=200)
    assert len(data) > 0
    # Проверяем, что пробуждение произошло ДО выполнения захвата скриншота
    wakeup_idx = next(i for i, c in enumerate(calls) if "KEYCODE_WAKEUP" in c)
    pull_idx = next(i for i, c in enumerate(calls) if "pull" in c)
    assert wakeup_idx < pull_idx


# --- ТЕСТЫ ИСПРАВЛЕНИЙ ЗАМЕЧАНИЙ РЕВЬЮВЕРА ---

@pytest.mark.asyncio
async def test_action_audit_on_ensure_awake_failure(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "dumpsys power" in cmd:
            raise actions.AdbTimeout("Превышен таймаут adb dumpsys power")
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.AdbTimeout):
        await actions.tap("realme", 100, 200)

    # Ошибка должна быть перехвачена _execute_action и записана в аудит с ok=False
    tap_entries = [e for e in setup_actions.entries if e["action"] == "tap"]
    assert len(tap_entries) == 1
    assert tap_entries[0]["ok"] is False
    assert "Превышен таймаут" in tap_entries[0]["error"]


@pytest.mark.asyncio
async def test_action_reconnect_on_ensure_awake_offline(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    actions._screen_cache.clear()
    connect_calls: list[tuple[str, int]] = []
    fail_first = True

    async def mock_connect(host: str, port: int) -> bool:
        connect_calls.append((host, port))
        return True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal fail_first
        if "dumpsys power" in cmd:
            if fail_first:
                fail_first = False
                raise actions.DeviceOffline("device offline")
            return "mWakefulness=Awake\nDisplay Power: state=ON\n"
        return ""

    monkeypatch.setattr(actions, "connect", mock_connect)
    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.tap("realme", 100, 200)

    assert len(connect_calls) >= 1
    tap_entries = [e for e in setup_actions.entries if e["action"] == "tap"]
    assert len(tap_entries) == 1
    assert tap_entries[0]["ok"] is True


@pytest.mark.asyncio
async def test_screenshot_audit_on_ensure_awake_failure(monkeypatch: pytest.MonkeyPatch, setup_actions: MockAudit):
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "dumpsys power" in cmd:
            raise actions.AdbTimeout("Превышен таймаут при проверке экрана")
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.AdbTimeout):
        await actions.screenshot("realme")

    screenshot_entries = [e for e in setup_actions.entries if e["action"] == "screenshot"]
    assert len(screenshot_entries) == 1
    assert screenshot_entries[0]["ok"] is False
    assert "Превышен таймаут" in screenshot_entries[0]["error"]


@pytest.mark.asyncio
async def test_ensure_awake_wait_loop_continues_on_exception(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache.clear()
    attempts = 0

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal attempts
        if "dumpsys power" in cmd:
            attempts += 1
            if attempts == 1:
                # Первый вызов проверки экрана - он выключен
                return "mWakefulness=Asleep\n"
            elif attempts == 2:
                # Первая итерация цикла ожидания падает с ошибкой
                raise RuntimeError("adb connection hiccup")
            elif attempts == 3:
                # Вторая итерация цикла ожидания возвращает включенный экран
                return "mWakefulness=Awake\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    res = await actions.ensure_awake("realme")
    assert res is True
    assert attempts >= 3
    assert actions._is_screen_cached_awake("realme") is True


@pytest.mark.asyncio
async def test_ensure_awake_timeout_does_not_cache_true(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "dumpsys power" in cmd:
            return "mWakefulness=Asleep\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    curr_time = 1000.0

    def mock_monotonic() -> float:
        return curr_time

    async def mock_sleep(sec: float):
        nonlocal curr_time
        curr_time += 1.0

    monkeypatch.setattr(actions.time, "monotonic", mock_monotonic)
    monkeypatch.setattr(actions.asyncio, "sleep", mock_sleep)

    res = await actions.ensure_awake("realme")
    assert res is True
    # При таймауте экран не должен быть закеширован как включенный
    assert "realme" not in actions._screen_cache
    assert actions._is_screen_cached_awake("realme") is False


@pytest.mark.asyncio
async def test_ensure_awake_raises_adb_timeout_no_false_cache(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache.clear()

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "dumpsys power" in cmd:
            raise actions.AdbTimeout("AdbTimeout in dumpsys power")
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.AdbTimeout):
        await actions.ensure_awake("realme")

    assert "realme" not in actions._screen_cache
    assert actions._is_screen_cached_awake("realme") is False


@pytest.mark.asyncio
async def test_ensure_awake_retry_after_offline_failure_raises(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache.clear()
    connect_called = False

    async def mock_connect(host: str, port: int) -> bool:
        nonlocal connect_called
        connect_called = True
        return False

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        raise actions.DeviceOffline("device offline")

    monkeypatch.setattr(actions, "connect", mock_connect)
    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    with pytest.raises(actions.DeviceOffline):
        await actions.ensure_awake("realme")

    assert connect_called is True
    assert "realme" not in actions._screen_cache
    assert actions._is_screen_cached_awake("realme") is False


@pytest.mark.asyncio
async def test_key_power_invalidates_screen_cache(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache["realme"] = (True, time.monotonic())
    assert actions._is_screen_cached_awake("realme") is True

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    await actions.key("realme", "power")
    assert "realme" not in actions._screen_cache
    assert actions._is_screen_cached_awake("realme") is False

    actions._screen_cache["realme"] = (True, time.monotonic())
    await actions.key("realme", "KEYCODE_POWER")
    assert "realme" not in actions._screen_cache
    assert actions._is_screen_cached_awake("realme") is False


@pytest.mark.asyncio
async def test_ensure_awake_parallel_calls_synchronization(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache.clear()
    state_asleep = True
    wake_events: list[str] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        nonlocal state_asleep
        if "dumpsys power" in cmd:
            if state_asleep:
                return "mWakefulness=Asleep\n"
            return "mWakefulness=Awake\n"
        if "KEYCODE_WAKEUP" in cmd:
            wake_events.append(cmd)
            await asyncio.sleep(0.05)
            state_asleep = False
            return ""
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    # Запускаем два параллельных ensure_awake
    res1, res2 = await asyncio.gather(
        actions.ensure_awake("realme"),
        actions.ensure_awake("realme"),
    )

    # Ровно один раз был вызван KEYCODE_WAKEUP
    assert len(wake_events) == 1
    # Первый вернул True (пришлось будить), второй подождал на Lock, увидел кеш awake и вернул False
    assert (res1 is True and res2 is False) or (res1 is False and res2 is True)
    assert actions._is_screen_cached_awake("realme") is True


@pytest.mark.asyncio
async def test_ensure_awake_recursion_protection_task_context(monkeypatch: pytest.MonkeyPatch):
    actions._screen_cache.clear()
    inner_res: list[bool] = []

    async def mock_adb_shell(serial: str, cmd: str, timeout: float = 15.0) -> str:
        if "dumpsys power" in cmd:
            # Имитируем рекурсивный вызов ensure_awake изнутри обработки
            r = await actions.ensure_awake("realme")
            inner_res.append(r)
            return "mWakefulness=Awake\nDisplay Power: state=ON\n"
        return ""

    monkeypatch.setattr(actions, "adb_shell", mock_adb_shell)

    outer_res = await actions.ensure_awake("realme")
    assert outer_res is False
    assert inner_res == [False]  # Рекурсивный вызов внутри той же задачи вернул False без взаимной блокировки




