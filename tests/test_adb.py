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

from hub.devices.adb import (
    AdbError,
    AdbTimeout,
    DeviceOffline,
    adb_shell,
    connect,
    run_adb,
)


class MockSubprocess:
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
    ):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = delay
        self.killed = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def mock_exec(monkeypatch):
    calls: list[tuple[tuple, dict]] = []

    def _setter(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, delay: float = 0.0):
        proc = MockSubprocess(stdout=stdout, stderr=stderr, returncode=returncode, delay=delay)

        async def _fake_exec(*cmd, **kwargs):
            calls.append((cmd, kwargs))
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return proc

    return _setter, calls


async def test_run_adb_text_success(mock_exec):
    setter, calls = mock_exec
    setter(stdout=b"Android Debug Bridge version 1.0.41\n", returncode=0)

    res = await run_adb(["version"])
    assert "Android Debug Bridge" in res
    assert calls[0][0] == ("adb", "version")


async def test_run_adb_binary_success(mock_exec):
    setter, calls = mock_exec
    sample_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    setter(stdout=sample_bytes, returncode=0)

    res = await run_adb(["exec-out", "screencap", "-p"], binary=True)
    assert res == sample_bytes
    assert isinstance(res, bytes)


async def test_run_adb_with_serial(mock_exec):
    setter, calls = mock_exec
    setter(stdout=b"ok\n", returncode=0)

    res = await run_adb(["shell", "echo", "1"], serial="100.64.0.1:5555")
    assert res == "ok\n"
    assert calls[0][0] == ("adb", "-s", "100.64.0.1:5555", "shell", "echo", "1")


async def test_run_adb_timeout(mock_exec):
    setter, calls = mock_exec
    proc = setter(delay=1.0)

    with pytest.raises(AdbTimeout) as exc_info:
        await run_adb(["shell", "sleep 10"], timeout=0.01)

    assert proc.killed is True
    assert "Превышено время ожидания" in str(exc_info.value)


@pytest.mark.parametrize(
    "stderr,stdout,returncode",
    [
        (b"error: device offline\n", b"", 1),
        (b"error: device '100.64.0.1:5555' not found\n", b"", 1),
        (b"error: no devices/emulators found\n", b"", 1),
        (b"", b"error: device offline\n", 0),
    ],
)
async def test_run_adb_device_offline(mock_exec, stderr, stdout, returncode):
    setter, _ = mock_exec
    setter(stdout=stdout, stderr=stderr, returncode=returncode)

    with pytest.raises(DeviceOffline) as exc_info:
        await run_adb(["shell", "ls"], serial="100.64.0.1:5555")

    assert issubclass(DeviceOffline, AdbError)
    assert "оффлайн или не найдено" in str(exc_info.value)


async def test_run_adb_generic_error(mock_exec):
    setter, _ = mock_exec
    setter(stderr=b"unknown command foo\n", returncode=1)

    with pytest.raises(AdbError) as exc_info:
        await run_adb(["foo"])

    assert "Ошибка выполнения ADB (код 1)" in str(exc_info.value)
    assert "unknown command foo" in str(exc_info.value)


async def test_run_adb_file_not_found(monkeypatch):
    async def _missing(*args, **kwargs):
        raise FileNotFoundError("adb not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _missing)

    with pytest.raises(AdbError) as exc_info:
        await run_adb(["version"])

    assert "ADB не найден" in str(exc_info.value)


async def test_run_adb_settings_path(monkeypatch, mock_exec):
    setter, calls = mock_exec
    setter(stdout=b"ok\n", returncode=0)

    import hub.config
    custom_settings = hub.config.Settings(adb_path="/custom/bin/adb")
    monkeypatch.setattr(hub.config, "get_settings", lambda: custom_settings)

    await run_adb(["devices"])
    assert calls[0][0][0] == "/custom/bin/adb"


async def test_adb_shell(mock_exec):
    setter, calls = mock_exec
    setter(stdout=b"test-output\n", returncode=0)

    res = await adb_shell("100.64.0.1:5555", "dumpsys battery")
    assert res == "test-output\n"
    assert calls[0][0] == ("adb", "-s", "100.64.0.1:5555", "shell", "dumpsys battery")


async def test_connect_success(mock_exec):
    setter, _ = mock_exec
    setter(stdout=b"connected to 100.64.0.1:5555\n", returncode=0)
    assert await connect("100.64.0.1", 5555) is True

    setter(stdout=b"already connected to 100.64.0.1:5555\n", returncode=0)
    assert await connect("100.64.0.1", 5555) is True


async def test_connect_failure(mock_exec):
    setter, _ = mock_exec
    setter(stdout=b"cannot connect to 100.64.0.1:5555: Connection refused\n", returncode=0)
    assert await connect("100.64.0.1", 5555) is False

    setter(stdout=b"failed to connect to '100.64.0.1:5555': Connection timed out\n", returncode=0)
    assert await connect("100.64.0.1", 5555) is False


async def test_connect_exception(monkeypatch):
    async def _failing(*args, **kwargs):
        raise AdbTimeout("Таймаут")

    monkeypatch.setattr("hub.devices.adb.run_adb", _failing)
    assert await connect("100.64.0.1", 5555) is False


async def test_run_adb_cancelled(mock_exec):
    setter, _ = mock_exec
    proc = setter(delay=5.0)

    task = asyncio.create_task(run_adb(["shell", "sleep 10"]))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed is True


async def test_run_adb_binary_offline_error(mock_exec):
    setter, _ = mock_exec
    setter(stdout=b"error: device offline\n", returncode=0)

    with pytest.raises(DeviceOffline) as exc_info:
        await run_adb(["exec-out", "screencap", "-p"], serial="100.64.0.1:5555", binary=True)

    assert "оффлайн или не найдено" in str(exc_info.value)


async def test_run_adb_device_not_found_with_serial(mock_exec):
    setter, _ = mock_exec
    setter(stdout=b"error: device '100.64.0.1:5555' not found\n", returncode=0)

    with pytest.raises(DeviceOffline) as exc_info:
        await run_adb(["shell", "dumpsys"], serial="100.64.0.1:5555")

    assert "оффлайн или не найдено" in str(exc_info.value)

