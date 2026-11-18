"""
tests/test_desktop_readiness_usb_probe.py

Тесты для desktop/readiness_usb_probe.py.
Используют фиктивный runner вместо реального adb.
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable
from unittest.mock import MagicMock

import pytest

from desktop.readiness_usb_probe import (
    ErrorCode,
    ProbeResult,
    USBProbeError,
    USBReadinessProbe,
    _parse_battery_level,
    _parse_devices,
    _is_usb,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции для создания фиктивных runner-ов
# ---------------------------------------------------------------------------

def _make_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


DEVICES_HEADER = "List of devices attached\n"

# Типичный вывод `adb devices -l` для USB-устройства
GOOD_DEVICES_OUTPUT = (
    DEVICES_HEADER
    + "ABCDEF1234567890       device transport_id:3 product:star2lte model:SM_G965F device:star2lte\n"
)

GOOD_GETPROP_OUTPUT = "13\n"

GOOD_DUMPSYS_OUTPUT = (
    "Current Battery Service state:\n"
    "  AC powered: false\n"
    "  USB powered: true\n"
    "  Wireless powered: false\n"
    "  Max charging current: 500000\n"
    "  Max charging voltage: 5000000\n"
    "  Charge counter: 2000000\n"
    "  status: 2\n"
    "  health: 2\n"
    "  present: true\n"
    "  level: 87\n"
    "  scale: 100\n"
    "  voltage: 4200\n"
    "  temperature: 280\n"
    "  technology: Li-ion\n"
)


def _build_runner(
    devices_out: str = GOOD_DEVICES_OUTPUT,
    devices_rc: int = 0,
    getprop_out: str = GOOD_GETPROP_OUTPUT,
    getprop_rc: int = 0,
    dumpsys_out: str = GOOD_DUMPSYS_OUTPUT,
    dumpsys_rc: int = 0,
    devices_stderr: str = "",
    getprop_stderr: str = "",
    dumpsys_stderr: str = "",
) -> Callable:
    """
    Строит фиктивный runner, который возвращает заготовленные ответы
    в зависимости от команды adb.
    """
    def runner(cmd: list[str], **kwargs):
        # cmd = ["adb", ...args...]
        args = cmd[1:]  # убираем "adb"
        if args == ["devices", "-l"]:
            return _make_result(devices_out, devices_stderr, devices_rc)
        # shell через -t <transport_id> shell <subcmd…>
        # или через -s <serial> shell <subcmd…>
        # Определяем подкоманду
        # Формат: [-t N | -s S] shell subcmd…
        joined = " ".join(args)
        if "shell" in args:
            shell_idx = args.index("shell")
            subcmd = args[shell_idx + 1:]
            if subcmd and subcmd[0] == "getprop":
                return _make_result(getprop_out, getprop_stderr, getprop_rc)
            if subcmd and subcmd[0] == "dumpsys":
                return _make_result(dumpsys_out, dumpsys_stderr, dumpsys_rc)
        return _make_result("", "неизвестная команда", 1)

    return runner


# ---------------------------------------------------------------------------
# Тесты парсинга
# ---------------------------------------------------------------------------

class TestParseDevices:
    def test_usb_device_parsed(self):
        entries = _parse_devices(GOOD_DEVICES_OUTPUT)
        assert len(entries) == 1
        e = entries[0]
        assert e.serial == "ABCDEF1234567890"
        assert e.state == "device"
        assert e.transport_id == 3

    def test_offline_device(self):
        out = DEVICES_HEADER + "DEADBEEF00000001       offline transport_id:1\n"
        entries = _parse_devices(out)
        assert entries[0].state == "offline"

    def test_unauthorized_device(self):
        out = DEVICES_HEADER + "DEADBEEF00000002       unauthorized transport_id:2\n"
        entries = _parse_devices(out)
        assert entries[0].state == "unauthorized"

    def test_network_device_not_usb(self):
        out = DEVICES_HEADER + "192.168.1.100:5555     device transport_id:5\n"
        entries = _parse_devices(out)
        assert len(entries) == 1
        assert not _is_usb(entries[0])

    def test_emulator_not_usb(self):
        out = DEVICES_HEADER + "emulator-5554          device transport_id:4\n"
        entries = _parse_devices(out)
        assert not _is_usb(entries[0])

    def test_multiple_devices(self):
        out = (
            DEVICES_HEADER
            + "AAA111                 device transport_id:1\n"
            + "BBB222                 device transport_id:2\n"
        )
        entries = _parse_devices(out)
        assert len(entries) == 2

    def test_empty_output(self):
        entries = _parse_devices(DEVICES_HEADER)
        assert entries == []

    def test_no_transport_id(self):
        out = DEVICES_HEADER + "SERIAL0001             device\n"
        entries = _parse_devices(out)
        assert entries[0].transport_id == -1


class TestParseBatteryLevel:
    def test_normal_level(self):
        assert _parse_battery_level(GOOD_DUMPSYS_OUTPUT) == 87

    def test_zero_level(self):
        assert _parse_battery_level("  level: 0\n") == 0

    def test_full_level(self):
        assert _parse_battery_level("  level: 100\n") == 100

    def test_clamped_above_100(self):
        # Выше 100 не бывает, но клипируем
        assert _parse_battery_level("  level: 150\n") == 100

    def test_clamped_below_0(self):
        assert _parse_battery_level("  level: -5\n") == 0

    def test_missing_level(self):
        assert _parse_battery_level("  status: 2\n  health: 2\n") is None

    def test_level_with_spaces(self):
        assert _parse_battery_level("    level:   42\n") == 42


# ---------------------------------------------------------------------------
# Тесты USBReadinessProbe.check_transport — успешный сценарий
# ---------------------------------------------------------------------------

class TestCheckTransportSuccess:
    def test_returns_probe_result(self):
        probe = USBReadinessProbe("adb", runner=_build_runner())
        result = probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert isinstance(result, ProbeResult)
        assert result.serial == "ABCDEF1234567890"
        assert result.transport_id == 3
        assert result.state == "device"
        assert result.android_version == "13"
        assert result.battery_level == 87

    def test_battery_level_range(self):
        probe = USBReadinessProbe("adb", runner=_build_runner(
            dumpsys_out="  level: 0\n"
        ))
        result = probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert 0 <= result.battery_level <= 100

    def test_android_version_stripped(self):
        probe = USBReadinessProbe("adb", runner=_build_runner(
            getprop_out="  11  \n"
        ))
        result = probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert result.android_version == "11"


# ---------------------------------------------------------------------------
# Тесты отклонения устройств
# ---------------------------------------------------------------------------

class TestCheckTransportRejections:
    def test_device_not_found(self):
        probe = USBReadinessProbe("adb", runner=_build_runner())
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("NONEXISTENT", timeout=10.0)
        assert exc_info.value.code == ErrorCode.USB_DISCONNECTED

    def test_device_offline(self):
        out = DEVICES_HEADER + "ABCDEF1234567890       offline transport_id:3\n"
        probe = USBReadinessProbe("adb", runner=_build_runner(devices_out=out))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.USB_DISCONNECTED

    def test_device_unauthorized(self):
        out = DEVICES_HEADER + "ABCDEF1234567890       unauthorized transport_id:3\n"
        probe = USBReadinessProbe("adb", runner=_build_runner(devices_out=out))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.ADB_UNAUTHORIZED

    def test_network_transport_rejected(self):
        out = DEVICES_HEADER + "192.168.1.100:5555     device transport_id:5\n"
        probe = USBReadinessProbe("adb", runner=_build_runner(devices_out=out))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("192.168.1.100:5555", timeout=10.0)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED

    def test_emulator_transport_rejected(self):
        out = DEVICES_HEADER + "emulator-5554          device transport_id:4\n"
        probe = USBReadinessProbe("adb", runner=_build_runner(devices_out=out))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("emulator-5554", timeout=10.0)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED

    def test_devices_command_error(self):
        probe = USBReadinessProbe("adb", runner=_build_runner(
            devices_out="", devices_rc=1, devices_stderr="daemon not running"
        ))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED

    def test_unknown_state_rejected(self):
        out = DEVICES_HEADER + "ABCDEF1234567890       connecting transport_id:3\n"
        probe = USBReadinessProbe("adb", runner=_build_runner(devices_out=out))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.USB_DISCONNECTED

    def test_no_transport_id_rejected(self):
        # transport_id не указан → -1 → PROBE_FAILED
        out = DEVICES_HEADER + "ABCDEF1234567890       device\n"
        probe = USBReadinessProbe("adb", runner=_build_runner(devices_out=out))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED


# ---------------------------------------------------------------------------
# Тесты таймаута
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_timeout_on_devices_command(self):
        """Таймаут при выполнении 'adb devices -l'."""
        def slow_runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        probe = USBReadinessProbe("adb", runner=slow_runner)
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=5.0)
        assert exc_info.value.code == ErrorCode.TIMEOUT

    def test_timeout_on_getprop(self):
        """Таймаут при выполнении getprop."""
        call_count = 0

        def runner_with_timeout(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            args = cmd[1:]
            if args == ["devices", "-l"]:
                return _make_result(GOOD_DEVICES_OUTPUT)
            # Следующий вызов (getprop) тайм-аутит
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        probe = USBReadinessProbe("adb", runner=runner_with_timeout)
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=5.0)
        assert exc_info.value.code == ErrorCode.TIMEOUT
        assert call_count == 2

    def test_zero_timeout_raises_immediately(self):
        """При timeout=0 бюджет уже исчерпан до первого вызова."""
        # Используем runner, который всегда успешен — но до него не должны доходить
        probe = USBReadinessProbe("adb", runner=_build_runner())
        # Перекрываем monotonic, чтобы deadline уже истёк к моменту вызова _run_adb
        original_monotonic = time.monotonic

        # Принудительно делаем timeout=0 — а значит deadline = now + 0 < now при вызове
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=0.0)
        assert exc_info.value.code == ErrorCode.TIMEOUT

    def test_budget_respects_one_timeout(self):
        """Единый бюджет: все вызовы делят один timeout."""
        calls: list[float] = []

        def recording_runner(cmd, **kwargs):
            calls.append(kwargs.get("timeout", -1))
            args = cmd[1:]
            if args == ["devices", "-l"]:
                return _make_result(GOOD_DEVICES_OUTPUT)
            if "getprop" in args:
                return _make_result(GOOD_GETPROP_OUTPUT)
            if "dumpsys" in args:
                return _make_result(GOOD_DUMPSYS_OUTPUT)
            return _make_result("", "неизвестно", 1)

        probe = USBReadinessProbe("adb", runner=recording_runner)
        probe.check_transport("ABCDEF1234567890", timeout=30.0)

        assert len(calls) == 3  # devices -l, getprop, dumpsys battery
        # Каждый вызов должен получить меньше бюджета, чем предыдущий
        for i in range(1, len(calls)):
            assert calls[i] <= calls[i - 1], (
                f"Вызов {i} получил больше времени, чем {i - 1}: {calls}"
            )
        # Суммарный бюджет не превышает исходный timeout
        # (на самом деле каждый отдельный вызов не превышает timeout)
        for t in calls:
            assert t <= 30.0


# ---------------------------------------------------------------------------
# Тесты удаления устройства (device removed / disconnected)
# ---------------------------------------------------------------------------

class TestDeviceRemoval:
    def test_device_disappears_between_devices_and_shell(self):
        """
        Устройство есть в `adb devices`, но shell-команда падает
        (устройство отключили в промежутке).
        """
        def runner(cmd, **kwargs):
            args = cmd[1:]
            if args == ["devices", "-l"]:
                return _make_result(GOOD_DEVICES_OUTPUT)
            # shell-команды: device not found
            return _make_result(
                "", "error: device 'ABCDEF1234567890' not found", 1
            )

        probe = USBReadinessProbe("adb", runner=runner)
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        # Должна быть ошибка PROBE_FAILED (не смогли выполнить shell)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED

    def test_empty_devices_list(self):
        """Нет ни одного устройства."""
        probe = USBReadinessProbe("adb", runner=_build_runner(
            devices_out=DEVICES_HEADER
        ))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.USB_DISCONNECTED


# ---------------------------------------------------------------------------
# Тесты парсинга батареи с разным выводом
# ---------------------------------------------------------------------------

class TestBatteryParsing:
    def test_battery_50(self):
        probe = USBReadinessProbe("adb", runner=_build_runner(
            dumpsys_out="  level: 50\n"
        ))
        result = probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert result.battery_level == 50

    def test_battery_missing_raises(self):
        probe = USBReadinessProbe("adb", runner=_build_runner(
            dumpsys_out="  status: 2\n  health: 2\n"
        ))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED

    def test_battery_command_fails(self):
        probe = USBReadinessProbe("adb", runner=_build_runner(
            dumpsys_out="", dumpsys_rc=1, dumpsys_stderr="error"
        ))
        with pytest.raises(USBProbeError) as exc_info:
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert exc_info.value.code == ErrorCode.PROBE_FAILED


# ---------------------------------------------------------------------------
# Тесты: нет shell на невалидном транспорте
# ---------------------------------------------------------------------------

class TestNoShellOnInvalidTransport:
    def test_no_shell_called_for_offline_device(self):
        """Для offline-устройства shell не вызывается."""
        shell_called = False

        def runner(cmd, **kwargs):
            nonlocal shell_called
            args = cmd[1:]
            if args == ["devices", "-l"]:
                out = DEVICES_HEADER + "ABCDEF1234567890       offline transport_id:3\n"
                return _make_result(out)
            if "shell" in args:
                shell_called = True
            return _make_result()

        probe = USBReadinessProbe("adb", runner=runner)
        with pytest.raises(USBProbeError):
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert not shell_called, "shell не должен вызываться для offline-устройства"

    def test_no_shell_called_for_unauthorized_device(self):
        """Для unauthorized-устройства shell не вызывается."""
        shell_called = False

        def runner(cmd, **kwargs):
            nonlocal shell_called
            args = cmd[1:]
            if args == ["devices", "-l"]:
                out = DEVICES_HEADER + "ABCDEF1234567890       unauthorized transport_id:3\n"
                return _make_result(out)
            if "shell" in args:
                shell_called = True
            return _make_result()

        probe = USBReadinessProbe("adb", runner=runner)
        with pytest.raises(USBProbeError):
            probe.check_transport("ABCDEF1234567890", timeout=10.0)
        assert not shell_called, "shell не должен вызываться для unauthorized-устройства"

    def test_no_shell_called_for_network_transport(self):
        """Для сетевого транспорта shell не вызывается."""
        shell_called = False

        def runner(cmd, **kwargs):
            nonlocal shell_called
            args = cmd[1:]
            if args == ["devices", "-l"]:
                out = DEVICES_HEADER + "192.168.1.100:5555     device transport_id:5\n"
                return _make_result(out)
            if "shell" in args:
                shell_called = True
            return _make_result()

        probe = USBReadinessProbe("adb", runner=runner)
        with pytest.raises(USBProbeError):
            probe.check_transport("192.168.1.100:5555", timeout=10.0)
        assert not shell_called, "shell не должен вызываться для сетевого транспорта"

    def test_transport_flag_used_in_shell(self):
        """Shell-команды должны использовать -t <transport_id>, не -s <serial>."""
        shell_args_list: list[list[str]] = []

        def runner(cmd, **kwargs):
            args = cmd[1:]
            if args == ["devices", "-l"]:
                return _make_result(GOOD_DEVICES_OUTPUT)
            if "shell" in args:
                shell_args_list.append(list(args))
                if "getprop" in args:
                    return _make_result("13")
                if "dumpsys" in args:
                    return _make_result("  level: 55\n")
            return _make_result()

        probe = USBReadinessProbe("adb", runner=runner)
        probe.check_transport("ABCDEF1234567890", timeout=10.0)

        assert len(shell_args_list) == 2
        for shell_args in shell_args_list:
            assert "-t" in shell_args, f"Ожидался флаг -t в аргументах: {shell_args}"
            # Убеждаемся, что -s не используется
            assert "-s" not in shell_args, f"Не должен использоваться -s: {shell_args}"
            # transport_id = 3
            t_idx = shell_args.index("-t")
            assert shell_args[t_idx + 1] == "3"


# ---------------------------------------------------------------------------
# Тесты error code enum
# ---------------------------------------------------------------------------

class TestErrorCodes:
    def test_all_codes_exist(self):
        assert ErrorCode.USB_DISCONNECTED
        assert ErrorCode.ADB_UNAUTHORIZED
        assert ErrorCode.TIMEOUT
        assert ErrorCode.PROBE_FAILED

    def test_error_has_code_attribute(self):
        err = USBProbeError(ErrorCode.TIMEOUT, "test")
        assert err.code == ErrorCode.TIMEOUT
        assert "test" in str(err)
