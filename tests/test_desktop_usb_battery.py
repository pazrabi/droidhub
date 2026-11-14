"""Тесты для изолированного модуля чтения заряда Android по USB (desktop/usb_battery.py).

Проверяют:
1. Успешное чтение заряда: точный вызов subprocess.run с аргументами
   ["-s", serial, "shell", "dumpsys", "battery"], shell=False, timeout=3.0.
2. Актуализацию перед исполнением: перечитывание registry.load() и adb.list_devices().
3. Замену привязки: корректное переключение на новый serial после изменения связи в реестре.
4. Отключение устройства: невозможность прочитать заряд при отсутствии устройства в ADB.
5. Неоднозначность (дубликаты serial): отказ при наличии нескольких устройств с одним serial.
6. Аппаратный USB: отсеивание сетевых (TCP/IP), не-device (unauthorized, offline),
   а также устройств без usb_info.
7. Непривязанные и некорректные идентификаторы устройств.
8. Ошибки подпроцесса: TimeoutExpired, FileNotFoundError, ненулевой returncode.
9. Безопасность сообщений об ошибках: гарантированное отсутствие stderr,
   серийных номеров и локальных путей в сообщениях USBBatteryError.
10. Лимиты вывода: пустой вывод (ошибка), размер более 16 КиБ (ошибка),
    корректная обработка UTF-8 и байтов.
11. Неизменность общей политики безопасности AdbClient._execute (shell запрещен).
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from desktop.adb_client import (
    AdbClient,
    AdbClientError,
    DeviceInfo,
)
from desktop.device_links import LinkRegistry
from desktop.usb_battery import (
    BATTERY_COMMAND_TIMEOUT,
    MAX_BATTERY_OUTPUT_BYTES,
    USBBatteryError,
    read_usb_battery,
)


# ---------------------------------------------------------------------------
# Вспомогательные генераторы тестовых данных и фикстуры
# ---------------------------------------------------------------------------


def make_test_device(
    serial: str = "USB_SERIAL_123",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    is_ready: bool | None = None,
    model: str | None = "Pixel 8a",
) -> DeviceInfo:
    """Создает валидный объект DeviceInfo для тестирования."""
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


SAMPLE_BATTERY_OUTPUT = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  Max charging current: 500000
  Max charging voltage: 5000000
  Charge counter: 2500000
  status: 2
  health: 2
  present: true
  level: 87
  scale: 100
  voltage: 4150
  temperature: 290
  technology: Li-ion
"""


@pytest.fixture
def temp_registry(tmp_path: Path) -> LinkRegistry:
    """Создает чистый временный реестр связей."""
    return LinkRegistry(tmp_path / "device_links.json")


# ---------------------------------------------------------------------------
# Тесты базовой функциональности и параметров вызова
# ---------------------------------------------------------------------------


class TestUsbBatterySuccess:
    """Тесты успешного чтения информации о батарее."""

    def test_successful_battery_read_str_stdout(self, temp_registry: LinkRegistry) -> None:
        """Проверяет успешное выполнение команды с возвратом строки stdout."""
        serial = "USB_SERIAL_001"
        hub_id = "hub_phone_01"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_BATTERY_OUTPUT
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            result = read_usb_battery(mock_adb, temp_registry, hub_id)

            assert result == SAMPLE_BATTERY_OUTPUT
            mock_run.assert_called_once_with(
                ["adb", "-s", serial, "shell", "dumpsys", "battery"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=BATTERY_COMMAND_TIMEOUT,
                shell=False,
                check=False,
            )

    def test_successful_battery_read_bytes_stdout(self, temp_registry: LinkRegistry) -> None:
        """Проверяет успешное декодирование байтового вывода из подпроцесса."""
        serial = "USB_SERIAL_BYTES"
        hub_id = "hub_phone_bytes"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/usr/bin/adb"
        mock_adb.list_devices.return_value = [dev]

        raw_bytes = SAMPLE_BATTERY_OUTPUT.encode("utf-8")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = raw_bytes
        mock_proc.stderr = b""

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            result = read_usb_battery(mock_adb, temp_registry, hub_id)
            assert result == SAMPLE_BATTERY_OUTPUT
            mock_run.assert_called_once_with(
                ["/usr/bin/adb", "-s", serial, "shell", "dumpsys", "battery"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3.0,
                shell=False,
                check=False,
            )


# ---------------------------------------------------------------------------
# Тесты актуализации реестра и списка устройств непосредственно перед исполнением
# ---------------------------------------------------------------------------


class TestReloadAndDynamicState:
    """Тесты перечитывания registry.load() и adb.list_devices() перед исполнением."""

    def test_registry_load_called_before_execution(self, tmp_path: Path) -> None:
        """Проверяет, что registry.load() вызывается непосредственно перед чтением."""
        reg_file = tmp_path / "links.json"
        registry1 = LinkRegistry(reg_file)
        registry2 = LinkRegistry(reg_file)

        # registry1 создает привязку на диске
        registry1.link("SERIAL_DYNAMIC", "hub_dynamic")

        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        dev = make_test_device(serial="SERIAL_DYNAMIC")
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=SAMPLE_BATTERY_OUTPUT, stderr="")

        # registry2 еще не загружен в память, но read_usb_battery вызовет registry.load()
        with patch("subprocess.run", return_value=mock_proc):
            result = read_usb_battery(mock_adb, registry2, "hub_dynamic")
            assert "level: 87" in result

    def test_binding_replacement_detected(self, tmp_path: Path) -> None:
        """Проверяет корректное применение замены привязки (нового серийного номера)."""
        reg_file = tmp_path / "links.json"
        registry = LinkRegistry(reg_file)
        hub_id = "hub_phone_replace"

        # Изначально привязан к SERIAL_OLD
        registry.link("SERIAL_OLD", hub_id)

        dev_old = make_test_device(serial="SERIAL_OLD")
        dev_new = make_test_device(serial="SERIAL_NEW")
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        mock_adb.list_devices.return_value = [dev_old, dev_new]

        mock_proc = MagicMock(returncode=0, stdout=SAMPLE_BATTERY_OUTPUT, stderr="")

        # Заменяем привязку: удаляем старую и привязываем к SERIAL_NEW
        registry.unlink(hub_id=hub_id)
        registry.link("SERIAL_NEW", hub_id)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            read_usb_battery(mock_adb, registry, hub_id)
            # Вызов должен быть выполнен на SERIAL_NEW, а не SERIAL_OLD
            args = mock_run.call_args[0][0]
            assert args == ["adb", "-s", "SERIAL_NEW", "shell", "dumpsys", "battery"]

    def test_unlinking_between_calls_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет, что удаление привязки перед повторным вызовом приводит к ошибке."""
        hub_id = "hub_phone_unlink"
        temp_registry.link("SERIAL_UNLINK", hub_id)

        dev = make_test_device(serial="SERIAL_UNLINK")
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]
        mock_proc = MagicMock(returncode=0, stdout=SAMPLE_BATTERY_OUTPUT, stderr="")

        with patch("subprocess.run", return_value=mock_proc):
            assert "level: 87" in read_usb_battery(mock_adb, temp_registry, hub_id)

        # Удаляем привязку
        temp_registry.unlink(hub_id=hub_id)

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBBatteryError, match="не найдено в реестре"):
                read_usb_battery(mock_adb, temp_registry, hub_id)


# ---------------------------------------------------------------------------
# Тесты неоднозначностей и отключения устройства
# ---------------------------------------------------------------------------


class TestDisconnectionAndDuplicates:
    """Тесты обработки отключенных устройств и дубликатов serial."""

    def test_device_disconnected_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет ошибку при отключении устройства (отсутствует в adb.list_devices())."""
        serial = "DISCONNECTED_SERIAL"
        hub_id = "hub_disconnected"
        temp_registry.link(serial, hub_id)

        mock_adb = MagicMock(spec=AdbClient)
        # ADB видит другие устройства, но не наше
        other_dev = make_test_device(serial="OTHER_SERIAL")
        mock_adb.list_devices.return_value = [other_dev]

        with patch("subprocess.run") as mock_run:
            with pytest.raises(USBBatteryError) as exc_info:
                read_usb_battery(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert serial not in err_msg
            mock_run.assert_not_called()

    def test_device_list_empty_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет ошибку при пустом списке подключенных устройств."""
        serial = "EMPTY_SERIAL"
        hub_id = "hub_empty"
        temp_registry.link(serial, hub_id)

        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = []

        with pytest.raises(USBBatteryError) as exc_info:
            read_usb_battery(mock_adb, temp_registry, hub_id)
        assert serial not in str(exc_info.value)

    def test_duplicate_serial_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ при обнаружении нескольких устройств с одинаковым serial."""
        serial = "DUPLICATE_SERIAL"
        hub_id = "hub_duplicate"
        temp_registry.link(serial, hub_id)

        dev1 = make_test_device(serial=serial, usb_info="1-1")
        dev2 = make_test_device(serial=serial, usb_info="1-2")
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev1, dev2]

        with patch("subprocess.run") as mock_run:
            with pytest.raises(USBBatteryError) as exc_info:
                read_usb_battery(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert "дубликат" in err_msg.lower()
            assert serial not in err_msg
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Тесты проверок аппаратного USB и готовности
# ---------------------------------------------------------------------------


class TestHardwareUsbEligibility:
    """Тесты строгих критериев аппаратного USB (is_network, usb_info, state, is_ready)."""

    def test_network_serial_device_rejected(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ для сетевого устройства (IP:порт)."""
        serial = "192.168.1.100:5555"
        hub_id = "hub_net_phone"
        # LinkRegistry запрещает link с IP:порт через validate_identifier, но проверим
        # случай, когда serial помечен как сетевой в DeviceInfo
        mock_reg = MagicMock(spec=LinkRegistry)
        mock_reg.load.return_value = []
        mock_reg.is_corrupted = False
        mock_reg.get_serial.return_value = "PHONE_SERIAL_NET"

        dev = make_test_device(serial="PHONE_SERIAL_NET", is_network=True)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        with pytest.raises(USBBatteryError) as exc_info:
            read_usb_battery(mock_adb, mock_reg, hub_id)
        assert "PHONE_SERIAL_NET" not in str(exc_info.value)

    def test_missing_usb_info_rejected(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ для устройства без аппаратного атрибута usb_info."""
        serial = "USB_NO_INFO"
        hub_id = "hub_no_usb_info"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, usb_info=None)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        with pytest.raises(USBBatteryError) as exc_info:
            read_usb_battery(mock_adb, temp_registry, hub_id)
        assert serial not in str(exc_info.value)

    def test_unauthorized_state_rejected(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ для неавторизованного устройства (state='unauthorized')."""
        serial = "USB_UNAUTH"
        hub_id = "hub_unauth"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, state="unauthorized", is_ready=False)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        with pytest.raises(USBBatteryError) as exc_info:
            read_usb_battery(mock_adb, temp_registry, hub_id)
        assert serial not in str(exc_info.value)

    def test_offline_state_rejected(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ для офлайн устройства (state='offline')."""
        serial = "USB_OFFLINE"
        hub_id = "hub_offline"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, state="offline", is_ready=False)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        with pytest.raises(USBBatteryError) as exc_info:
            read_usb_battery(mock_adb, temp_registry, hub_id)
        assert serial not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Тесты безопасности: отсутствие утечки stderr, путей и серийников
# ---------------------------------------------------------------------------


class TestErrorSecurityAndRedaction:
    """Тесты отсутствия конфиденциальных данных в сообщениях об ошибках."""

    def test_subprocess_error_does_not_leak_stderr_serial_or_paths(
        self, temp_registry: LinkRegistry
    ) -> None:
        """Проверяет, что при ненулевом коде возврата stderr, serial и путь adb не попадают в сообщение."""
        serial = "SECRET_SERIAL_XYZ"
        hub_id = "hub_secret"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/opt/secret/bin/adb"
        mock_adb.list_devices.return_value = [dev]

        secret_stderr = f"adb: error: device '{serial}' not found at /opt/secret/bin/adb"
        mock_proc = MagicMock(returncode=1, stdout="", stderr=secret_stderr)

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBBatteryError) as exc_info:
                read_usb_battery(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert serial not in err_msg
            assert "/opt/secret/bin/adb" not in err_msg
            assert secret_stderr not in err_msg
            assert "код 1" in err_msg

    def test_subprocess_timeout_expired_no_leak(self, temp_registry: LinkRegistry) -> None:
        """Проверяет обработку TimeoutExpired без утечки параметров."""
        serial = "TIMEOUT_SERIAL"
        hub_id = "hub_timeout"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/home/user/android-tools/adb"
        mock_adb.list_devices.return_value = [dev]

        cmd = [mock_adb.adb_path, "-s", serial, "shell", "dumpsys", "battery"]
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=cmd, timeout=3.0)):
            with pytest.raises(USBBatteryError) as exc_info:
                read_usb_battery(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert serial not in err_msg
            assert "/home/user" not in err_msg
            assert "ожидания" in err_msg

    def test_adb_binary_not_found_no_leak(self, temp_registry: LinkRegistry) -> None:
        """Проверяет обработку FileNotFoundError без утечки локального пути к исполняемому файлу."""
        serial = "NOTFOUND_SERIAL"
        hub_id = "hub_notfound"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        custom_path = "/custom/local/path/to/missing_adb"
        mock_adb.adb_path = custom_path
        mock_adb.list_devices.return_value = [dev]

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file or directory", custom_path)):
            with pytest.raises(USBBatteryError) as exc_info:
                read_usb_battery(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert custom_path not in err_msg
            assert serial not in err_msg
            assert "не найден" in err_msg

    def test_corrupted_registry_no_path_leak(self, tmp_path: Path) -> None:
        """Проверяет обработку поврежденного файла реестра без утечки пути."""
        bad_file = tmp_path / "corrupted_links.json"
        bad_file.write_text("{invalid json", encoding="utf-8")
        registry = LinkRegistry(bad_file)

        mock_adb = MagicMock(spec=AdbClient)

        with pytest.raises(USBBatteryError) as exc_info:
            read_usb_battery(mock_adb, registry, "any_device_id")

        err_msg = str(exc_info.value)
        assert str(bad_file) not in err_msg


# ---------------------------------------------------------------------------
# Тесты ограничений размера вывода и валидации пустого результата
# ---------------------------------------------------------------------------


class TestOutputLimitsAndEmptiness:
    """Тесты лимита 16 КиБ UTF-8 и отсеивания пустого вывода."""

    def test_empty_string_stdout_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ при абсолютно пустой строке вывода."""
        serial = "USB_EMPTY_OUT"
        hub_id = "hub_empty_out"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBBatteryError, match="пустой вывод"):
                read_usb_battery(mock_adb, temp_registry, hub_id)

    def test_whitespace_only_stdout_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ при выводе, состоящем только из пробелов и переводов строк."""
        serial = "USB_WS_OUT"
        hub_id = "hub_ws_out"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout="   \n\t  \r\n", stderr="")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBBatteryError, match="пустой вывод"):
                read_usb_battery(mock_adb, temp_registry, hub_id)

    def test_output_exceeding_16_kib_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ при размере вывода свыше 16 КиБ (16384 байт)."""
        serial = "USB_HUGE_OUT"
        hub_id = "hub_huge_out"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        # 16 КиБ + 1 байт
        huge_stdout = "x" * (16 * 1024 + 1)
        mock_proc = MagicMock(returncode=0, stdout=huge_stdout, stderr="")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBBatteryError, match="16 КиБ"):
                read_usb_battery(mock_adb, temp_registry, hub_id)

    def test_output_exactly_16_kib_succeeds(self, temp_registry: LinkRegistry) -> None:
        """Проверяет успешное чтение при размере ровно 16 КиБ (16384 байт)."""
        serial = "USB_MAX_OUT"
        hub_id = "hub_max_out"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        exact_16k_stdout = "x" * (16 * 1024)
        mock_proc = MagicMock(returncode=0, stdout=exact_16k_stdout, stderr="")

        with patch("subprocess.run", return_value=mock_proc):
            result = read_usb_battery(mock_adb, temp_registry, hub_id)
            assert len(result.encode("utf-8")) == 16 * 1024

    def test_invalid_utf8_in_bytes_output_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ при выводе недопустимых байтов UTF-8."""
        serial = "USB_INVALID_UTF8"
        hub_id = "hub_invalid_utf8"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=b"\x80\x81\xff\xfe", stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBBatteryError, match="UTF-8"):
                read_usb_battery(mock_adb, temp_registry, hub_id)


# ---------------------------------------------------------------------------
# Тесты безопасности: AdbClient._execute остается строгим и неизменным
# ---------------------------------------------------------------------------


class TestAdbClientPolicyPreservation:
    """Тесты гарантии того, что общая политика AdbClient._execute не ослаблена."""

    def test_adb_client_execute_still_forbids_shell(self) -> None:
        """Проверяет, что AdbClient._execute по-прежнему запрещает команду shell."""
        client = AdbClient()
        with pytest.raises(AdbClientError, match="запрещена"):
            client._execute(["shell", "dumpsys", "battery"])

    def test_read_usb_battery_does_not_modify_forbidden_commands(self) -> None:
        """Проверяет, что список запрещенных команд AdbClient не был изменен."""
        from desktop.adb_client import FORBIDDEN_ADB_COMMANDS

        assert "shell" in FORBIDDEN_ADB_COMMANDS
        assert "root" in FORBIDDEN_ADB_COMMANDS
        assert "connect" in FORBIDDEN_ADB_COMMANDS
        assert "tcpip" in FORBIDDEN_ADB_COMMANDS


# ---------------------------------------------------------------------------
# Тесты граничных условий и валидации входных параметров
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Тесты проверки входных аргументов функции read_usb_battery."""

    def test_none_adb_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ при передаче adb=None."""
        with pytest.raises(USBBatteryError, match="клиент ADB"):
            read_usb_battery(None, temp_registry, "hub_id")  # type: ignore[arg-type]

    def test_none_registry_raises_error(self) -> None:
        """Проверяет отказ при передаче registry=None."""
        mock_adb = MagicMock(spec=AdbClient)
        with pytest.raises(USBBatteryError, match="реестр связей"):
            read_usb_battery(mock_adb, None, "hub_id")  # type: ignore[arg-type]

    @pytest.mark.parametrize("invalid_id", ["", "   ", None, 123, True])
    def test_invalid_device_id_raises_error(
        self, temp_registry: LinkRegistry, invalid_id: object
    ) -> None:
        """Проверяет отказ при некорректном идентификаторе устройства."""
        mock_adb = MagicMock(spec=AdbClient)
        with pytest.raises(USBBatteryError, match="идентификатор"):
            read_usb_battery(mock_adb, temp_registry, invalid_id)  # type: ignore[arg-type]

    def test_unlinked_device_id_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Проверяет отказ, если hub_id не привязан ни к одному серийному номеру."""
        mock_adb = MagicMock(spec=AdbClient)
        with pytest.raises(USBBatteryError, match="не найдено в реестре"):
            read_usb_battery(mock_adb, temp_registry, "unlinked_hub_id")
