"""Тесты для изолированного модуля операций экрана и тапа по USB (desktop/usb_screen.py).

Проверяют:
1. Захват экрана (capture_usb_screen):
   - вызов subprocess.run с аргументами ["exec-out", "screencap", "-p"], shell=False, timeout=4.0;
   - адресация через -t <transport_id>, если transport_id доступен в снимке ADB;
   - адресация через -s <serial>, если transport_id отсутствует или пуст;
   - успешный возврат исходных байтов PNG.
2. Нажатие на экран (tap_usb_screen):
   - вызов subprocess.run с аргументами ["shell", "input", "tap", str(x), str(y)], shell=False, timeout=4.0;
   - адресация через -t <transport_id> или -s <serial>;
   - возврат None при успешном выполнении.
3. Валидацию координат:
   - строго int: запрет bool (True, False), float, str, None;
   - диапазон: строго 0..16383 (-1 и 16384 вызывают ошибку);
   - без повторов: совпадение X и Y (x == y) вызывает ошибку.
4. Актуализацию перед каждым действием:
   - обязательный вызов registry.load() и adb.list_devices();
   - учет изменений связей на диске;
   - отключение устройства между вызовами.
5. Фильтрацию и валидацию устройств:
   - ровно одно совпадение по serial;
   - запрет дубликатов (ambiguity);
   - отсеивание сетевых устройств (is_network == True или IP:port в serial);
   - проверка готовности аппаратного USB (usb_info, state == 'device', is_ready == True).
6. Лимиты и валидацию вывода PNG:
   - пустой вывод (ошибка);
   - размер более 8 МиБ (ошибка);
   - проверка магической сигнатуры PNG (b"\\x89PNG\\r\\n\\x1a\\n");
   - усеченные или некорректные заголовки.
7. Безопасность сообщений об ошибках:
   - гарантированное отсутствие stderr, серийных номеров и локальных путей файловой системы;
   - корректная обработка TimeoutExpired (4 с), FileNotFoundError, ненулевых returncode.
8. Отсутствие повторных попыток (без повторов):
   - единичный вызов subprocess.run без ретраев при ошибках.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from desktop.adb_client import (
    AdbClient,
    DeviceInfo,
)
from desktop.device_links import LinkRegistry
from desktop.usb_screen import (
    MAX_PNG_BYTES,
    PNG_SIGNATURE,
    SCREEN_COMMAND_TIMEOUT,
    USBScreenError,
    capture_usb_screen,
    tap_usb_screen,
)


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры и генераторы тестовых данных
# ---------------------------------------------------------------------------


def make_test_device(
    serial: str = "USB_SERIAL_123",
    state: str = "device",
    usb_info: str | None = "1-1.2",
    is_network: bool = False,
    is_ready: bool | None = None,
    model: str | None = "Pixel 8a",
    transport_id: str | None = None,
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
        transport_id=transport_id,
        usb_info=usb_info,
    )


VALID_PNG_BYTES = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"


@pytest.fixture
def temp_registry(tmp_path: Path) -> LinkRegistry:
    """Создает чистый временный реестр связей."""
    return LinkRegistry(tmp_path / "device_links.json")


# ---------------------------------------------------------------------------
# Тесты захвата экрана (capture_usb_screen)
# ---------------------------------------------------------------------------


class TestCaptureUsbScreen:
    """Тесты функции захвата экрана."""

    def test_capture_success_with_serial(self, temp_registry: LinkRegistry) -> None:
        """Успешный захват экрана без transport_id: адресация через -s <serial>."""
        serial = "USB_SERIAL_CAP_01"
        hub_id = "phone_screen_01"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id=None)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=VALID_PNG_BYTES, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            result = capture_usb_screen(mock_adb, temp_registry, hub_id)

            assert result == VALID_PNG_BYTES
            mock_run.assert_called_once_with(
                ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=SCREEN_COMMAND_TIMEOUT,
                shell=False,
                check=False,
            )

    def test_capture_success_with_transport_id(self, temp_registry: LinkRegistry) -> None:
        """Успешный захват экрана с transport_id: адресация через -t <transport_id>."""
        serial = "USB_SERIAL_CAP_02"
        hub_id = "phone_screen_02"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id="42")
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/usr/bin/adb"
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=VALID_PNG_BYTES, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            result = capture_usb_screen(mock_adb, temp_registry, hub_id)

            assert result == VALID_PNG_BYTES
            mock_run.assert_called_once_with(
                ["/usr/bin/adb", "-t", "42", "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=4.0,
                shell=False,
                check=False,
            )

    def test_capture_with_empty_or_whitespace_transport_id_falls_back_to_serial(
        self, temp_registry: LinkRegistry
    ) -> None:
        """Пустой или состоящий из пробелов transport_id откатывается к -s <serial>."""
        serial = "USB_SERIAL_CAP_03"
        hub_id = "phone_screen_03"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id="   ")
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=VALID_PNG_BYTES, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            result = capture_usb_screen(mock_adb, temp_registry, hub_id)
            assert result == VALID_PNG_BYTES
            assert mock_run.call_args[0][0][1:3] == ["-s", serial]


# ---------------------------------------------------------------------------
# Тесты нажатия по экрану (tap_usb_screen)
# ---------------------------------------------------------------------------


class TestTapUsbScreen:
    """Тесты функции нажатия по экрану."""

    def test_tap_success_with_transport_id(self, temp_registry: LinkRegistry) -> None:
        """Успешный тап с transport_id: аргументы ["shell", "input", "tap", X, Y]."""
        serial = "USB_SERIAL_TAP_01"
        hub_id = "phone_tap_01"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id="101")
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            tap_usb_screen(mock_adb, temp_registry, hub_id, 300, 750)

            mock_run.assert_called_once_with(
                ["adb", "-t", "101", "shell", "input", "tap", "300", "750"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=SCREEN_COMMAND_TIMEOUT,
                shell=False,
                check=False,
            )

    def test_tap_success_with_serial(self, temp_registry: LinkRegistry) -> None:
        """Успешный тап без transport_id: адресация через -s <serial>."""
        serial = "USB_SERIAL_TAP_02"
        hub_id = "phone_tap_02"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id=None)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            tap_usb_screen(mock_adb, temp_registry, hub_id, 0, 16383)

            mock_run.assert_called_once_with(
                ["adb", "-s", serial, "shell", "input", "tap", "0", "16383"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=SCREEN_COMMAND_TIMEOUT,
                shell=False,
                check=False,
            )


# ---------------------------------------------------------------------------
# Тесты строгой валидации координат тапа
# ---------------------------------------------------------------------------


class TestCoordinateValidation:
    """Тесты строгой валидации координат тапа: int (без bool), 0..16383, без повторов."""

    def test_coordinate_bool_rejected(self, temp_registry: LinkRegistry) -> None:
        """Координаты типа bool (True/False) строго запрещены."""
        mock_adb = MagicMock(spec=AdbClient)

        with pytest.raises(USBScreenError, match="целым числом"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", True, 100)

        with pytest.raises(USBScreenError, match="целым числом"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 100, False)

        with pytest.raises(USBScreenError, match="целым числом"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", False, True)

    def test_coordinate_non_int_rejected(self, temp_registry: LinkRegistry) -> None:
        """Координаты типов float, str, None строго запрещены."""
        mock_adb = MagicMock(spec=AdbClient)

        with pytest.raises(USBScreenError, match="целым числом"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 10.5, 200)

        with pytest.raises(USBScreenError, match="целым числом"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 100, "200")  # type: ignore

        with pytest.raises(USBScreenError, match="целым числом"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", None, 200)  # type: ignore

    def test_coordinate_out_of_bounds_rejected(self, temp_registry: LinkRegistry) -> None:
        """Координаты за пределами диапазона 0..16383 запрещены."""
        mock_adb = MagicMock(spec=AdbClient)

        with pytest.raises(USBScreenError, match="диапазон"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", -1, 500)

        with pytest.raises(USBScreenError, match="диапазон"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 500, -1)

        with pytest.raises(USBScreenError, match="диапазон"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 16384, 500)

        with pytest.raises(USBScreenError, match="диапазон"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 500, 16384)

    def test_coordinate_repeats_rejected(self, temp_registry: LinkRegistry) -> None:
        """Повторяющиеся координаты (X == Y) строго запрещены («без повторов»)."""
        mock_adb = MagicMock(spec=AdbClient)

        with pytest.raises(USBScreenError, match="повтор"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 100, 100)

        with pytest.raises(USBScreenError, match="повтор"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 0, 0)

        with pytest.raises(USBScreenError, match="повтор"):
            tap_usb_screen(mock_adb, temp_registry, "dev1", 16383, 16383)


# ---------------------------------------------------------------------------
# Тесты актуализации реестра и ADB перед каждым действием
# ---------------------------------------------------------------------------


class TestReloadAndDynamicState:
    """Тесты актуализации реестра и списка устройств перед каждым вызовом."""

    def test_registry_loaded_before_each_action(self, tmp_path: Path) -> None:
        """Реестр перечитывается с диска перед вызовом."""
        reg_file = tmp_path / "links.json"
        reg1 = LinkRegistry(reg_file)
        reg2 = LinkRegistry(reg_file)

        reg1.link("SERIAL_DYNAMIC", "hub_dynamic")

        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        dev = make_test_device(serial="SERIAL_DYNAMIC")
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=VALID_PNG_BYTES, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            res = capture_usb_screen(mock_adb, reg2, "hub_dynamic")
            assert res == VALID_PNG_BYTES

    def test_binding_replacement_detected(self, tmp_path: Path) -> None:
        """Замена привязки устройства на новый serial применяется сразу."""
        reg_file = tmp_path / "links.json"
        registry = LinkRegistry(reg_file)
        hub_id = "phone_replace"

        registry.link("SERIAL_OLD", hub_id)

        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "adb"
        dev_new = make_test_device(serial="SERIAL_NEW")
        mock_adb.list_devices.return_value = [dev_new]

        # Перепривязываем на SERIAL_NEW: сначала unlink, затем link
        registry.unlink(hub_id=hub_id)
        registry.link("SERIAL_NEW", hub_id)

        mock_proc = MagicMock(returncode=0, stdout=VALID_PNG_BYTES, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            capture_usb_screen(mock_adb, registry, hub_id)
            assert mock_run.call_args[0][0][1:3] == ["-s", "SERIAL_NEW"]

    def test_unlinking_before_action_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Удаление связи перед выполнением приводит к ошибке."""
        hub_id = "phone_unlink"
        temp_registry.link("SERIAL_UNLINK", hub_id)

        mock_adb = MagicMock(spec=AdbClient)
        temp_registry.unlink(hub_id=hub_id)

        with pytest.raises(USBScreenError, match="не найдено в реестре"):
            capture_usb_screen(mock_adb, temp_registry, hub_id)

        with pytest.raises(USBScreenError, match="не найдено в реестре"):
            tap_usb_screen(mock_adb, temp_registry, hub_id, 100, 200)


# ---------------------------------------------------------------------------
# Тесты валидации устройств и защиты от сетевого ADB / дубликатов
# ---------------------------------------------------------------------------


class TestDeviceValidationAndSecurity:
    """Тесты проверки ровно одного аппаратного USB и запрета сетевого ADB."""

    def test_device_not_found_in_adb(self, temp_registry: LinkRegistry) -> None:
        """Ошибка, если привязанное устройство отсутствует в списке подключенных."""
        serial = "USB_NOT_CONNECTED"
        hub_id = "phone_offline"
        temp_registry.link(serial, hub_id)

        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = []

        with pytest.raises(USBScreenError, match="не обнаружено среди подключенных"):
            capture_usb_screen(mock_adb, temp_registry, hub_id)

    def test_duplicate_devices_in_adb_rejected(self, temp_registry: LinkRegistry) -> None:
        """Неоднозначность (дубликаты серийного номера) приводит к ошибке."""
        serial = "DUPLICATE_SERIAL"
        hub_id = "phone_dupe"
        temp_registry.link(serial, hub_id)

        dev1 = make_test_device(serial=serial)
        dev2 = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev1, dev2]

        with pytest.raises(USBScreenError, match="дубликаты"):
            capture_usb_screen(mock_adb, temp_registry, hub_id)

    def test_network_adb_device_rejected(self, temp_registry: LinkRegistry) -> None:
        """Сетевые устройства (TCP/IP) строго запрещены."""
        serial = "192.168.1.100:5555"
        hub_id = "phone_network"

        # Привязка сетевого серийника должна быть отклонена
        with patch.object(temp_registry, "get_serial", return_value=serial):
            mock_adb = MagicMock(spec=AdbClient)
            dev = make_test_device(serial=serial, is_network=True)
            mock_adb.list_devices.return_value = [dev]

            with pytest.raises(USBScreenError, match="сетевым ADB"):
                capture_usb_screen(mock_adb, temp_registry, hub_id)

    def test_ineligible_usb_device_rejected(self, temp_registry: LinkRegistry) -> None:
        """Неготовые устройства (offline, unauthorized, без usb_info) отклоняются."""
        serial = "USB_INELIGIBLE"
        hub_id = "phone_ineligible"
        temp_registry.link(serial, hub_id)

        mock_adb = MagicMock(spec=AdbClient)

        # 1. unauthorized
        dev_unauth = make_test_device(serial=serial, state="unauthorized", is_ready=False)
        mock_adb.list_devices.return_value = [dev_unauth]
        with pytest.raises(USBScreenError, match="не готово к работе"):
            capture_usb_screen(mock_adb, temp_registry, hub_id)

        # 2. offline
        dev_offline = make_test_device(serial=serial, state="offline", is_ready=False)
        mock_adb.list_devices.return_value = [dev_offline]
        with pytest.raises(USBScreenError, match="не готово к работе"):
            tap_usb_screen(mock_adb, temp_registry, hub_id, 10, 20)

        # 3. no usb_info
        dev_no_usb = make_test_device(serial=serial, usb_info=None)
        mock_adb.list_devices.return_value = [dev_no_usb]
        with pytest.raises(USBScreenError, match="не является аппаратным USB"):
            capture_usb_screen(mock_adb, temp_registry, hub_id)


# ---------------------------------------------------------------------------
# Тесты лимитов и сигнатуры PNG для capture_usb_screen
# ---------------------------------------------------------------------------


class TestPngValidationAndLimits:
    """Тесты проверки лимитов и сигнатуры PNG."""

    def test_empty_output_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Пустой вывод от устройства вызывает исключение."""
        serial = "USB_EMPTY"
        hub_id = "phone_empty"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBScreenError, match="пустой вывод"):
                capture_usb_screen(mock_adb, temp_registry, hub_id)

    def test_output_exceeding_8mib_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Вывод размером более 8 МиБ вызывает исключение."""
        serial = "USB_HUGE"
        hub_id = "phone_huge"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        huge_data = PNG_SIGNATURE + b"\x00" * (MAX_PNG_BYTES + 1 - len(PNG_SIGNATURE))
        mock_proc = MagicMock(returncode=0, stdout=huge_data, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBScreenError, match="превышает допустимый лимит 8 МиБ"):
                capture_usb_screen(mock_adb, temp_registry, hub_id)

    def test_invalid_png_signature_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Данные с неверной сигнатурой PNG вызывают исключение."""
        serial = "USB_BAD_SIGNATURE"
        hub_id = "phone_bad_sig"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        # 1. Неверная сигнатура (JPEG вместо PNG)
        jpeg_mock = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        mock_proc = MagicMock(returncode=0, stdout=jpeg_mock, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBScreenError, match="неверная сигнатура"):
                capture_usb_screen(mock_adb, temp_registry, hub_id)

        # 2. Слишком короткие данные (< 8 байт)
        short_mock = b"\x89PNG"
        mock_proc = MagicMock(returncode=0, stdout=short_mock, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBScreenError, match="неверная сигнатура"):
                capture_usb_screen(mock_adb, temp_registry, hub_id)


# ---------------------------------------------------------------------------
# Тесты безопасности сообщений об ошибках и подпроцесса
# ---------------------------------------------------------------------------


class TestErrorSecurityAndRedaction:
    """Тесты отсутствия конфиденциальных данных в сообщениях об ошибках."""

    def test_subprocess_error_does_not_leak_stderr_serial_or_paths(
        self, temp_registry: LinkRegistry
    ) -> None:
        """При ненулевом коде возврата stderr, serial и путь adb не попадают в сообщение."""
        serial = "SECRET_SERIAL_XYZ"
        hub_id = "phone_secret"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/opt/secret/bin/adb"
        mock_adb.list_devices.return_value = [dev]

        secret_stderr = f"adb: error: device '{serial}' not found at /opt/secret/bin/adb"
        mock_proc = MagicMock(returncode=1, stdout=b"", stderr=secret_stderr.encode("utf-8"))

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(USBScreenError) as exc_info:
                capture_usb_screen(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert serial not in err_msg
            assert "/opt/secret/bin/adb" not in err_msg
            assert secret_stderr not in err_msg
            assert "код 1" in err_msg

    def test_subprocess_timeout_expired_no_leak(self, temp_registry: LinkRegistry) -> None:
        """Обработка TimeoutExpired (4 с) без утечки параметров."""
        serial = "TIMEOUT_SERIAL"
        hub_id = "phone_timeout"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/home/user/android-sdk/platform-tools/adb"
        mock_adb.list_devices.return_value = [dev]

        cmd = [mock_adb.adb_path, "-s", serial, "exec-out", "screencap", "-p"]
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=cmd, timeout=4.0)):
            with pytest.raises(USBScreenError) as exc_info:
                capture_usb_screen(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert serial not in err_msg
            assert "/home/user" not in err_msg
            assert "ожидания" in err_msg

    def test_adb_not_found_no_leak(self, temp_registry: LinkRegistry) -> None:
        """Обработка FileNotFoundError без утечки локального пути к adb."""
        serial = "NOTFOUND_SERIAL"
        hub_id = "phone_notfound"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        custom_path = "/custom/local/missing_adb"
        mock_adb.adb_path = custom_path
        mock_adb.list_devices.return_value = [dev]

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file", custom_path)):
            with pytest.raises(USBScreenError) as exc_info:
                tap_usb_screen(mock_adb, temp_registry, hub_id, 100, 200)

            err_msg = str(exc_info.value)
            assert custom_path not in err_msg
            assert serial not in err_msg
            assert "не найден" in err_msg

    def test_tap_subprocess_error_no_retries(self, temp_registry: LinkRegistry) -> None:
        """При ошибке подпроцесса тап завершается без повторов (call_count == 1)."""
        serial = "NO_RETRY_SERIAL"
        hub_id = "phone_no_retry"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=1, stdout=b"", stderr=b"error")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            with pytest.raises(USBScreenError):
                tap_usb_screen(mock_adb, temp_registry, hub_id, 100, 200)

            assert mock_run.call_count == 1

    def test_permission_error_no_leak(self, temp_registry: LinkRegistry) -> None:
        """Обработка PermissionError без утечки путей или параметров."""
        serial = "PERM_SERIAL"
        hub_id = "phone_perm"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/secret/adb"
        mock_adb.list_devices.return_value = [dev]

        with patch("subprocess.run", side_effect=PermissionError(13, "Permission denied", "/secret/adb")):
            with pytest.raises(USBScreenError) as exc_info:
                capture_usb_screen(mock_adb, temp_registry, hub_id)

            err_msg = str(exc_info.value)
            assert "/secret/adb" not in err_msg
            assert serial not in err_msg
            assert "доступе" in err_msg

    def test_os_error_no_leak(self, temp_registry: LinkRegistry) -> None:
        """Обработка OSError без утечки путей или параметров."""
        serial = "OS_SERIAL"
        hub_id = "phone_os"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.adb_path = "/var/adb"
        mock_adb.list_devices.return_value = [dev]

        with patch("subprocess.run", side_effect=OSError(5, "Input/output error")):
            with pytest.raises(USBScreenError) as exc_info:
                tap_usb_screen(mock_adb, temp_registry, hub_id, 10, 20)

            err_msg = str(exc_info.value)
            assert "/var/adb" not in err_msg
            assert serial not in err_msg
            assert "операционной системы" in err_msg


# ---------------------------------------------------------------------------
# Тесты базовой валидации входных параметров и реестра
# ---------------------------------------------------------------------------


class TestInputAndRegistryValidation:
    """Тесты валидации базовых аргументов adb, registry, device_id."""

    def test_missing_adb_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Отсутствие adb клиента вызывает USBScreenError."""
        with pytest.raises(USBScreenError, match="Не указан клиент ADB"):
            capture_usb_screen(None, temp_registry, "hub1")  # type: ignore

        with pytest.raises(USBScreenError, match="Не указан клиент ADB"):
            tap_usb_screen(None, temp_registry, "hub1", 10, 20)  # type: ignore

    def test_missing_registry_raises_error(self) -> None:
        """Отсутствие реестра вызывает USBScreenError."""
        mock_adb = MagicMock(spec=AdbClient)
        with pytest.raises(USBScreenError, match="Не указан реестр связей"):
            capture_usb_screen(mock_adb, None, "hub1")  # type: ignore

        with pytest.raises(USBScreenError, match="Не указан реестр связей"):
            tap_usb_screen(mock_adb, None, "hub1", 10, 20)  # type: ignore

    def test_invalid_device_id_raises_error(self, temp_registry: LinkRegistry) -> None:
        """Некорректный device_id вызывает USBScreenError."""
        mock_adb = MagicMock(spec=AdbClient)

        for bad_id in ("", "   ", None, 123, True, "bad id with spaces\n"):
            with pytest.raises(USBScreenError, match="Некорректный идентификатор"):
                capture_usb_screen(mock_adb, temp_registry, bad_id)  # type: ignore

            with pytest.raises(USBScreenError, match="Некорректный идентификатор"):
                tap_usb_screen(mock_adb, temp_registry, bad_id, 10, 20)  # type: ignore

    def test_corrupted_registry_raises_error_without_path_leak(self, tmp_path: Path) -> None:
        """Поврежденный файл реестра вызывает USBScreenError без утечки пути."""
        bad_file = tmp_path / "secret_corrupted.json"
        bad_file.write_text("{broken json", encoding="utf-8")
        registry = LinkRegistry(bad_file)
        mock_adb = MagicMock(spec=AdbClient)

        with pytest.raises(USBScreenError) as exc_info:
            capture_usb_screen(mock_adb, registry, "phone_hub_01")

        err_msg = str(exc_info.value)
        assert str(bad_file) not in err_msg

    def test_adb_list_devices_exception_handled(self, temp_registry: LinkRegistry) -> None:
        """Ошибка при вызове adb.list_devices корректно перехватывается."""
        serial = "DEV_ERR"
        hub_id = "phone_adb_err"
        temp_registry.link(serial, hub_id)

        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.side_effect = RuntimeError("ADB server crashed at /opt/adb")

        with pytest.raises(USBScreenError) as exc_info:
            capture_usb_screen(mock_adb, temp_registry, hub_id)

        err_msg = str(exc_info.value)
        assert "/opt/adb" not in err_msg
        assert serial not in err_msg
        assert "Не удалось получить список устройств ADB" in err_msg


# ---------------------------------------------------------------------------
# Тесты граничных условий форматов вывода и transport_id
# ---------------------------------------------------------------------------


class TestAdvancedOutputAndTransportId:
    """Дополнительные тесты форматов вывода и различных типов transport_id."""

    def test_exact_8mib_png_succeeds(self, temp_registry: LinkRegistry) -> None:
        """Снимок экрана ровно максимального допустимого размера (8 МиБ) успешен."""
        serial = "USB_EXACT_8MIB"
        hub_id = "phone_exact_8mib"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        exact_data = PNG_SIGNATURE + b"\x00" * (MAX_PNG_BYTES - len(PNG_SIGNATURE))
        assert len(exact_data) == MAX_PNG_BYTES
        mock_proc = MagicMock(returncode=0, stdout=exact_data, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            res = capture_usb_screen(mock_adb, temp_registry, hub_id)
            assert len(res) == MAX_PNG_BYTES
            assert res == exact_data

    def test_bytearray_output_accepted(self, temp_registry: LinkRegistry) -> None:
        """Вывод типа bytearray корректно преобразуется в bytes."""
        serial = "USB_BYTEARRAY"
        hub_id = "phone_bytearray"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial)
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        raw_ba = bytearray(VALID_PNG_BYTES)
        mock_proc = MagicMock(returncode=0, stdout=raw_ba, stderr=b"")

        with patch("subprocess.run", return_value=mock_proc):
            res = capture_usb_screen(mock_adb, temp_registry, hub_id)
            assert isinstance(res, bytes)
            assert res == VALID_PNG_BYTES

    def test_integer_transport_id_formatted_correctly(self, temp_registry: LinkRegistry) -> None:
        """Целочисленный transport_id корректно форматируется в строку для флага -t."""
        serial = "USB_INT_TRANSPORT"
        hub_id = "phone_int_trans"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id=77)  # type: ignore
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            tap_usb_screen(mock_adb, temp_registry, hub_id, 10, 20)
            cmd = mock_run.call_args[0][0]
            assert cmd[1:3] == ["-t", "77"]

    def test_bool_transport_id_ignored_falls_back_to_serial(self, temp_registry: LinkRegistry) -> None:
        """Булевый transport_id (например True) игнорируется, используется -s <serial>."""
        serial = "USB_BOOL_TRANSPORT"
        hub_id = "phone_bool_trans"
        temp_registry.link(serial, hub_id)

        dev = make_test_device(serial=serial, transport_id=True)  # type: ignore
        mock_adb = MagicMock(spec=AdbClient)
        mock_adb.list_devices.return_value = [dev]

        mock_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            tap_usb_screen(mock_adb, temp_registry, hub_id, 10, 20)
            cmd = mock_run.call_args[0][0]
            assert cmd[1:3] == ["-s", serial]
