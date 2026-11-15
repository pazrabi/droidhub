"""Безопасный исполнитель операции чтения заряда Android-устройства по USB.

Модуль предоставляет изолированную реализацию вызова `adb shell dumpsys battery`
для локальных аппаратных USB-устройств, привязанных к DroidHub, без изменения
общей политики безопасности AdbClient._execute.
"""

from __future__ import annotations

from collections.abc import Sequence
import subprocess

from desktop.adb_client import AdbClient, DeviceInfo
from desktop.device_links import (
    DeviceLinkError,
    LinkRegistry,
    validate_identifier,
)
from desktop.link_service import (
    IncompatibleDeviceError,
    check_usb_device_eligibility,
)
from desktop.process_flags import NO_CONSOLE

# Максимальный допустимый размер вывода команды в байтах UTF-8 (16 КиБ)
MAX_BATTERY_OUTPUT_BYTES: int = 16 * 1024

# Строго фиксированный таймаут выполнения команды чтения заряда (3 секунды)
BATTERY_COMMAND_TIMEOUT: float = 3.0

# Строго фиксированные аргументы вызова ADB для чтения информации о батарее
BATTERY_ADB_SHELL_ARGS: tuple[str, ...] = ("shell", "dumpsys", "battery")


class USBBatteryError(Exception):
    """Исключение при выполнении операции чтения заряда Android по USB."""
    pass


def read_usb_battery(
    adb: AdbClient,
    registry: LinkRegistry,
    device_id: str,
) -> str:
    """Выполняет локальное чтение заряда Android-устройства через ADB.

    Параметры:
        adb: экземпляр AdbClient для взаимодействия с ADB-сервером.
        registry: реестр явных USB-привязок LinkRegistry.
        device_id: идентификатор устройства (DroidHub ID).

    Возвращает:
        Строку с выводом команды `dumpsys battery` (не более 16 КиБ UTF-8).

    Исключения:
        USBBatteryError: при любой ошибке валидации, отсутствия привязки,
        отключения устройства, неоднозначности, неподходящем устройстве,
        ошибке выполнения команды или превышении лимитов.
        Сообщение исключения гарантированно не содержит stderr, серийных
        номеров или локальных путей файловой системы.
    """
    if adb is None:
        raise USBBatteryError("Не указан клиент ADB")

    if registry is None:
        raise USBBatteryError("Не указан реестр связей")

    if not isinstance(device_id, str) or not device_id.strip():
        raise USBBatteryError("Некорректный идентификатор устройства")

    try:
        validate_identifier(device_id, name="device_id")
    except Exception:
        raise USBBatteryError("Некорректный идентификатор устройства") from None

    # Непосредственно перед исполнением перечитываем реестр
    try:
        registry.load()
    except Exception:
        raise USBBatteryError("Не удалось прочитать реестр связей") from None

    if getattr(registry, "is_corrupted", False):
        raise USBBatteryError("Реестр связей поврежден")

    try:
        serial = registry.get_serial(device_id)
    except DeviceLinkError:
        raise USBBatteryError("Не удалось прочитать реестр связей") from None

    if not serial or not isinstance(serial, str) or not serial.strip():
        raise USBBatteryError("Устройство не найдено в реестре связей")

    try:
        validate_identifier(serial, name="serial")
    except Exception:
        raise USBBatteryError("Некорректная привязка устройства в реестре") from None

    # Непосредственно перед исполнением опрашиваем список устройств ADB
    try:
        devices = adb.list_devices()
    except Exception:
        raise USBBatteryError("Не удалось получить список устройств ADB") from None

    if not isinstance(devices, Sequence) or isinstance(devices, (str, bytes)):
        raise USBBatteryError("Не удалось получить список устройств ADB")

    # Поиск ровно одного DeviceInfo с данным serial
    matches = [d for d in devices if getattr(d, "serial", None) == serial]
    if len(matches) == 0:
        raise USBBatteryError("Привязанное устройство не обнаружено среди подключенных")
    if len(matches) > 1:
        raise USBBatteryError("Обнаружены дубликаты устройства в ADB")

    device_info = matches[0]
    if not isinstance(device_info, DeviceInfo):
        raise USBBatteryError("Некорректный формат данных об устройстве")

    # Проверка готовности аппаратного USB
    try:
        check_usb_device_eligibility(device_info)
    except IncompatibleDeviceError:
        raise USBBatteryError("Устройство не готово к работе или не является аппаратным USB") from None
    except Exception:
        raise USBBatteryError("Устройство не готово к работе или не является аппаратным USB") from None

    # Определение пути к adb
    adb_path = getattr(adb, "adb_path", "adb")
    if not isinstance(adb_path, str) or not adb_path.strip():
        adb_path = "adb"

    # Строго фиксированный вызов ADB без shell=True
    cmd = [adb_path, "-s", serial, *BATTERY_ADB_SHELL_ARGS]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=BATTERY_COMMAND_TIMEOUT,
            shell=False,
            check=False,
            **NO_CONSOLE,
        )
    except subprocess.TimeoutExpired:
        raise USBBatteryError("Превышено время ожидания ответа от устройства") from None
    except FileNotFoundError:
        raise USBBatteryError("Исполняемый файл ADB не найден") from None
    except PermissionError:
        raise USBBatteryError("Отказано в доступе при запуске команды ADB") from None
    except OSError:
        raise USBBatteryError("Ошибка операционной системы при запуске ADB") from None
    except Exception:
        raise USBBatteryError("Сбой при выполнении команды ADB") from None

    if proc.returncode != 0:
        raise USBBatteryError(f"Команда ADB завершилась с ошибкой (код {proc.returncode})") from None

    raw_stdout = proc.stdout
    if isinstance(raw_stdout, bytes):
        if len(raw_stdout) > MAX_BATTERY_OUTPUT_BYTES:
            raise USBBatteryError("Размер вывода превышает допустимый лимит 16 КиБ")
        try:
            output = raw_stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise USBBatteryError("Вывод команды содержит некорректный UTF-8") from None
    elif isinstance(raw_stdout, str):
        output = raw_stdout
    else:
        raise USBBatteryError("Получен некорректный вывод команды")

    if not output or not output.strip():
        raise USBBatteryError("Получен пустой вывод от устройства")

    if len(output.encode("utf-8")) > MAX_BATTERY_OUTPUT_BYTES:
        raise USBBatteryError("Размер вывода превышает допустимый лимит 16 КиБ")

    return output


__all__ = ["read_usb_battery", "USBBatteryError"]
