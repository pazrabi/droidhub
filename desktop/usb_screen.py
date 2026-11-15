"""Безопасный исполнитель операций захвата экрана и тапа по USB для телефона владельца.

Модуль предоставляет изолированную реализацию двух строго фиксированных операций:
1. `capture_usb_screen`: захват снимка экрана через `adb exec-out screencap -p`.
2. `tap_usb_screen`: нажатие по координатам через `adb shell input tap X Y`.

Обе операции предназначены исключительно для локальных аппаратных USB Android-устройств,
явно привязанных в реестре LinkRegistry, без изменения общей политики безопасности AdbClient.
"""

from __future__ import annotations

from collections.abc import Sequence
import subprocess
from typing import Any

from desktop.adb_client import AdbClient, DeviceInfo, is_network_serial
from desktop.device_links import LinkRegistry, validate_identifier
from desktop.link_service import (
    IncompatibleDeviceError,
    check_usb_device_eligibility,
)
from desktop.process_flags import NO_CONSOLE

# Строго фиксированный таймаут выполнения подпроцесса ADB (4 секунды)
SCREEN_COMMAND_TIMEOUT: float = 4.0

# Максимальный допустимый размер PNG в байтах (8 МиБ)
MAX_PNG_BYTES: int = 8 * 1024 * 1024

# Стандартная сигнатура (магические байты) формата PNG
PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"

# Границы допустимых значений координат
MIN_COORD: int = 0
MAX_COORD: int = 16383


class USBScreenError(Exception):
    """Исключение при выполнении операций захвата экрана или тапа по USB."""
    pass


def _validate_basic_args(adb: Any, registry: Any, device_id: Any) -> None:
    """Проверяет базовые параметры вызова до обращения к внешним сущностям."""
    if adb is None:
        raise USBScreenError("Не указан клиент ADB")

    if registry is None:
        raise USBScreenError("Не указан реестр связей")

    if not isinstance(device_id, str) or not device_id.strip():
        raise USBScreenError("Некорректный идентификатор устройства")

    try:
        validate_identifier(device_id, name="device_id")
    except Exception:
        raise USBScreenError("Некорректный идентификатор устройства") from None


def _validate_coordinates(x: Any, y: Any) -> None:
    """Проверяет координаты тапа: строго int (без bool), диапазон 0..16383."""
    if isinstance(x, bool) or not isinstance(x, int):
        raise USBScreenError("Координата X должна быть целым числом (int)")
    if isinstance(y, bool) or not isinstance(y, int):
        raise USBScreenError("Координата Y должна быть целым числом (int)")

    if not (MIN_COORD <= x <= MAX_COORD):
        raise USBScreenError(f"Координата X выходит за допустимый диапазон {MIN_COORD}..{MAX_COORD}")
    if not (MIN_COORD <= y <= MAX_COORD):
        raise USBScreenError(f"Координата Y выходит за допустимый диапазон {MIN_COORD}..{MAX_COORD}")


def _resolve_target_device(
    adb: AdbClient,
    registry: LinkRegistry,
    device_id: str,
) -> tuple[str, list[str]]:
    """Актуализирует реестр и список ADB, проверяет готовность аппаратного USB и формирует адресацию.

    Возвращает:
        Кортеж (serial, target_args), где target_args равен:
        ["-t", transport_id] если transport_id доступен в свежем снимке ADB,
        иначе ["-s", serial].
    """
    _validate_basic_args(adb, registry, device_id)

    # Непосредственно перед каждым действием перечитываем реестр
    try:
        registry.load()
    except Exception:
        raise USBScreenError("Не удалось прочитать реестр связей") from None

    if getattr(registry, "is_corrupted", False):
        raise USBScreenError("Реестр связей поврежден")

    try:
        serial = registry.get_serial(device_id)
    except Exception:
        raise USBScreenError("Не удалось прочитать реестр связей") from None

    if not serial or not isinstance(serial, str) or not serial.strip():
        raise USBScreenError("Устройство не найдено в реестре связей")

    try:
        validate_identifier(serial, name="serial")
    except Exception:
        raise USBScreenError("Некорректная привязка устройства в реестре") from None

    # Не обращаемся к сетевому ADB
    if is_network_serial(serial):
        raise USBScreenError("Устройство является сетевым ADB и запрещено к использованию")

    # Непосредственно перед каждым действием опрашиваем список устройств ADB
    try:
        devices = adb.list_devices()
    except Exception:
        raise USBScreenError("Не удалось получить список устройств ADB") from None

    if not isinstance(devices, Sequence) or isinstance(devices, (str, bytes)):
        raise USBScreenError("Не удалось получить список устройств ADB")

    # Перепроверяем ровно одно совпадение с подключенным устройством
    matches = [d for d in devices if getattr(d, "serial", None) == serial]
    if len(matches) == 0:
        raise USBScreenError("Привязанное устройство не обнаружено среди подключенных")
    if len(matches) > 1:
        raise USBScreenError("Обнаружены дубликаты устройства в ADB")

    device_info = matches[0]
    if not isinstance(device_info, DeviceInfo):
        raise USBScreenError("Некорректный формат данных об устройстве")

    if getattr(device_info, "is_network", False) or is_network_serial(device_info.serial):
        raise USBScreenError("Устройство является сетевым ADB и запрещено к использованию")

    # Проверка готовности аппаратного USB
    try:
        check_usb_device_eligibility(device_info)
    except IncompatibleDeviceError:
        raise USBScreenError("Устройство не готово к работе или не является аппаратным USB") from None
    except Exception:
        raise USBScreenError("Устройство не готово к работе или не является аппаратным USB") from None

    # Если доступен ADB transport_id из этого свежего снимка, адресуй его через -t, иначе -s
    transport_id = getattr(device_info, "transport_id", None)
    if transport_id is not None and not isinstance(transport_id, bool) and str(transport_id).strip():
        target_args = ["-t", str(transport_id).strip()]
    else:
        target_args = ["-s", serial]

    return serial, target_args


def capture_usb_screen(
    adb: AdbClient,
    registry: LinkRegistry,
    device_id: str,
) -> bytes:
    """Выполняет захват экрана локального USB Android-устройства через ADB exec-out screencap -p.

    Параметры:
        adb: экземпляр AdbClient для взаимодействия с локальным ADB-сервером.
        registry: реестр явных USB-привязок LinkRegistry.
        device_id: идентификатор устройства (DroidHub ID).

    Возвращает:
        Байты изображения в формате PNG (не более 8 МиБ с валидной сигнатурой PNG).

    Исключения:
        USBScreenError: при ошибках валидации, отсутствия привязки, неподходящем устройстве,
        превышении таймаута, ошибке подпроцесса, неверной сигнатуре или превышении лимита 8 МиБ.
        Сообщение исключения гарантированно не содержит stderr, серийных номеров или локальных путей.
    """
    _serial, target_args = _resolve_target_device(adb, registry, device_id)

    adb_path = getattr(adb, "adb_path", "adb")
    if not isinstance(adb_path, str) or not adb_path.strip():
        adb_path = "adb"

    cmd = [adb_path, *target_args, "exec-out", "screencap", "-p"]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SCREEN_COMMAND_TIMEOUT,
            shell=False,
            check=False,
            **NO_CONSOLE,
        )
    except subprocess.TimeoutExpired:
        raise USBScreenError("Превышено время ожидания ответа от устройства") from None
    except FileNotFoundError:
        raise USBScreenError("Исполняемый файл ADB не найден") from None
    except PermissionError:
        raise USBScreenError("Отказано в доступе при запуске команды ADB") from None
    except OSError:
        raise USBScreenError("Ошибка операционной системы при запуске ADB") from None
    except Exception:
        raise USBScreenError("Сбой при выполнении команды ADB") from None

    if proc.returncode != 0:
        raise USBScreenError(f"Команда ADB завершилась с ошибкой (код {proc.returncode})") from None

    raw_stdout = proc.stdout
    if isinstance(raw_stdout, (bytes, bytearray)):
        raw_bytes = bytes(raw_stdout)
    elif isinstance(raw_stdout, str):
        raw_bytes = raw_stdout.encode("latin1", errors="replace")
    else:
        raise USBScreenError("Некорректный формат вывода команды (ожидались байты)")

    if len(raw_bytes) == 0:
        raise USBScreenError("Получен пустой вывод от устройства")

    if len(raw_bytes) > MAX_PNG_BYTES:
        raise USBScreenError("Размер снимка экрана превышает допустимый лимит 8 МиБ")

    if not raw_bytes.startswith(PNG_SIGNATURE):
        raise USBScreenError("Полученные данные не являются корректным изображением PNG (неверная сигнатура)")

    return raw_bytes


def tap_usb_screen(
    adb: AdbClient,
    registry: LinkRegistry,
    device_id: str,
    x: int,
    y: int,
) -> None:
    """Выполняет нажатие (тап) по экрану локального USB Android-устройства через ADB shell input tap X Y.

    Параметры:
        adb: экземпляр AdbClient для взаимодействия с локальным ADB-сервером.
        registry: реестр явных USB-привязок LinkRegistry.
        device_id: идентификатор устройства (DroidHub ID).
        x: координата X (строго int 0..16383, без повторов).
        y: координата Y (строго int 0..16383, без повторов).

    Возвращает:
        None при успешном выполнении команды.

    Исключения:
        USBScreenError: при ошибках валидации аргументов и координат, неверном статусе,
        отсутствии привязки, превышении таймаута или ошибке выполнения команды подпроцессом.
        Сообщение исключения гарантированно не содержит stderr, серийных номеров или локальных путей.
    """
    _validate_coordinates(x, y)
    _serial, target_args = _resolve_target_device(adb, registry, device_id)

    adb_path = getattr(adb, "adb_path", "adb")
    if not isinstance(adb_path, str) or not adb_path.strip():
        adb_path = "adb"

    cmd = [adb_path, *target_args, "shell", "input", "tap", str(x), str(y)]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SCREEN_COMMAND_TIMEOUT,
            shell=False,
            check=False,
            **NO_CONSOLE,
        )
    except subprocess.TimeoutExpired:
        raise USBScreenError("Превышено время ожидания ответа от устройства") from None
    except FileNotFoundError:
        raise USBScreenError("Исполняемый файл ADB не найден") from None
    except PermissionError:
        raise USBScreenError("Отказано в доступе при запуске команды ADB") from None
    except OSError:
        raise USBScreenError("Ошибка операционной системы при запуске ADB") from None
    except Exception:
        raise USBScreenError("Сбой при выполнении команды ADB") from None

    if proc.returncode != 0:
        raise USBScreenError(f"Команда ADB завершилась с ошибкой (код {proc.returncode})") from None


__all__ = [
    "capture_usb_screen",
    "tap_usb_screen",
    "USBScreenError",
    "SCREEN_COMMAND_TIMEOUT",
    "MAX_PNG_BYTES",
    "PNG_SIGNATURE",
    "MIN_COORD",
    "MAX_COORD",
]
